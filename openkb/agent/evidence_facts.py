"""Validate and retain useful extraction independently of transport batch boundaries."""

import json
import re
import threading

from openkb.agent.evidence_coverage import require_unit_coverage
from openkb.agent.evidence_fact_cache import FactCache
from openkb.agent.evidence_parallel import parallel_batches
from openkb.agent.evidence_quotes import fact_quote
from openkb.agent.evidence_retry import ResponseIncomplete, retry_batches, split_units
from openkb.agent.evidence_units import (
    FACTS_SYSTEM,
    JSON_FORMAT,
    fact_batches,
    messages,
    source_units,
)
from openkb.config import compilation_model_options
from openkb.processing import ProcessingIncomplete, processing_checkpoint
from openkb.progress import progress_scope
from openkb.sources import content_id


def validate_unit(unit, item):
    extracted = item.get("facts")
    if not isinstance(extracted, list) or (
        not extracted
        and (not isinstance(item.get("empty_reason"), str) or not item["empty_reason"].strip())
    ):
        raise ResponseIncomplete("section_empty_without_reason", "facts")
    text = re.sub(r"!?\[[^\]]*\]\(asset:[0-9a-f]{64}\)", "", unit["text"])
    if (
        not extracted
        and unit["kind"] not in {"heading", "image"}
        and re.search(
            r"\d\s*(?:kPa|MPa|[kKmMgG]?[bB]|秒|分钟|小时|%|℃)\b|"
            r"\b(?:must|required|never|shall|only|unless|unsupported|timeout)\b|"
            r"必须|禁止|不得|不能|至少|不支持|前提|条件|需要",
            text,
            re.I,
        )
    ):
        raise ResponseIncomplete("source_facts_missing", "facts", unit_id=unit["id"])
    facts = []
    for fact in extracted:
        quote, start, end = fact_quote(unit, fact)
        reference = dict(unit["reference"])
        reference["start"] += start
        reference["end"] = unit["reference"]["start"] + end
        value = {
            "topic": fact["topic"],
            "statement": fact["statement"],
            "quote": quote,
            "reference": reference,
            "scope": unit["reference"],
            "context_evidence": [
                {"reference": neighbor["reference"], "relation": neighbor["relation"]}
                for neighbor in [*unit["heading_evidence"], *unit["neighbors"]]
            ],
        }
        facts.append({"id": content_id(value), **value})
    return facts


def extract_facts(
    kb_dir, source, parsed, settings, limits, checkpoints, bundle, on_event, *, navigation=None
):
    from openkb.agent.compiler import _llm_call

    model = settings["model"]
    processing_checkpoint("facts")
    units = list(source_units(kb_dir, source, parsed, limits, model, navigation=navigation))
    cache = FactCache(checkpoints, FACTS_SYSTEM, units, validate_unit)
    progress_lock = threading.Lock()
    failures = []

    def failed(batch, error):
        with progress_lock:
            failures.append((batch, error))
        on_event(
            {
                "stage": "facts",
                "operation": "unit_pending",
                "reason": error.reason,
                "units": len(batch),
            }
        )

    def extract(batch):
        processing_checkpoint("facts")
        cache.set_batch(batch)
        cached = {unit["id"]: cache.get(unit) for unit in batch}
        pending = [unit for unit in batch if cached[unit["id"]] is None]
        on_event(
            {
                "stage": "facts",
                "blocks": len(batch),
                "cached": not pending,
                "cached_blocks": len(batch) - len(pending),
                "analysis_hits": sum(unit["id"] in cache.shared_hits for unit in batch),
            }
        )
        from openkb.agent.analysis_flights import claimed
        from openkb.agent.shared_analysis import fact_input

        # Cache dependencies include every semantic row the model can see. When
        # recovered rows leave a smaller request, compare that exact request too.
        while pending:
            cache.set_batch(pending)
            missing = [unit for unit in pending if cache.get(unit) is None]
            if len(missing) == len(pending):
                break
            pending = missing
        claim_payload = {"request": [fact_input(unit) for unit in pending]}
        with claimed(cache.shared, [claim_payload] if pending else []) as claims:
            owned = pending if claims and claims[0] is not None and claims[0].owner else []
            # A previous executor may have finished between the first lookup and claim.
            if owned and all(cache.get(unit) is not None for unit in owned):
                owned = []
            if owned:
                try:
                    raw = _llm_call(
                        model,
                        messages(FACTS_SYSTEM, {"stage": "facts", "units": owned}),
                        "facts",
                        bundle=bundle,
                        response_format=JSON_FORMAT,
                        **compilation_model_options(settings),
                    )
                    result = json.loads(raw)
                except (ValueError, TypeError):
                    raise ResponseIncomplete("evidence_output_invalid", "facts") from None
                outputs = result.get("units") if isinstance(result, dict) else None
                expected = {unit["id"]: unit for unit in owned}
                # Retain individually valid rows even if a different row is malformed.
                if isinstance(outputs, list):
                    counts = {}
                    good = {}
                    for row in outputs:
                        if isinstance(row, dict) and isinstance(row.get("id"), str):
                            counts[row["id"]] = counts.get(row["id"], 0) + 1
                    if any(uid not in expected or count != 1 for uid, count in counts.items()):
                        require_unit_coverage(outputs, expected)
                    for row in outputs:
                        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                            continue
                        uid = row["id"]
                        if uid not in expected or counts[uid] != 1:
                            continue
                        try:
                            validate_unit(expected[uid], row)
                        except ResponseIncomplete:
                            continue
                        good[uid] = row
                    if good:
                        valid = [unit for unit in owned if unit["id"] in good]
                        cache.save(valid, [good[unit["id"]] for unit in valid], receipt=raw)
                require_unit_coverage(outputs, expected)
                for row in outputs:
                    validate_unit(expected[row["id"]], row)
        for flight in claims:
            if flight is not None and not flight.owner:
                flight.wait("facts")
        if any(cache.get(unit) is None for unit in batch):
            raise ResponseIncomplete("evidence_output_invalid", "facts")
        return [fact for unit in batch for fact in validate_unit(unit, cache.get(unit))]

    def run_batch(batch):
        results = []
        for completed, facts in retry_batches(
            batch,
            extract,
            stage="facts",
            on_event=on_event,
            split=split_units,
            validation_attempts=limits.max_attempts,
            checkpoints=checkpoints,
            recovery_key=lambda batch: checkpoints.key(
                FACTS_SYSTEM, {"stage": "facts", "units": batch}
            ),
            on_unrecoverable=failed,
        ):
            with progress_lock:
                progress.advance(sum(len(unit["text"]) for unit in completed))
            results.extend(facts)
        return results

    with progress_scope(
        "facts", sum(len(unit["text"]) for unit in units), "characters"
    ) as progress:
        results = dict(
            parallel_batches(fact_batches(units, limits, model), run_batch, limits.concurrency)
        )
    facts = [fact for index in sorted(results) for fact in results[index]]
    if failures:
        from openkb.compilation_report import report_content_omission

        # A split sibling from a failed block must not turn an incomplete
        # extraction into an apparently complete source contribution.
        excluded = {unit["reference"]["block_id"] for batch, _ in failures for unit in batch}
        facts = [fact for fact in facts if fact["scope"]["block_id"] not in excluded]
        if not facts:
            raise failures[0][1]
        for batch, error in failures:
            report_content_omission(
                "facts", error.reason, [unit["reference"]["block_id"] for unit in batch]
            )
    if not facts and any(unit["kind"] not in {"image", "heading"} for unit in units):
        raise ProcessingIncomplete("source_facts_missing", "facts")
    return facts
