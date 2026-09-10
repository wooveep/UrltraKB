"""Compile complete structural evidence into one proposed source contribution."""

from __future__ import annotations

import json

from openkb.agent.evidence_checkpoints import CompilationCheckpoints
from openkb.agent.evidence_units import (
    FACTS_SYSTEM,
    JSON_FORMAT,
    fact_batches,
    messages,
    source_units,
)
from openkb.config import compilation_model_options
from openkb.evidence import ParseStore
from openkb.processing import ProcessingIncomplete, RequestLimits, processing_checkpoint
from openkb.progress import progress_scope
from openkb.sources import content_id


def _object(raw):
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
    except (ValueError, TypeError):
        pass
    raise ProcessingIncomplete("evidence_output_invalid", "facts")


def compile_evidence(
    kb_dir, workspace, source, parsed, name, settings, *, bundle=None, on_event=lambda event: None
):
    from openkb.agent.compiler import (
        _llm_call,
        _update_index,
        _write_concept,
        _write_entity,
        _write_summary,
    )
    from openkb.lint import list_existing_wiki_targets

    limits = RequestLimits.from_config(settings)
    model = settings["model"]
    reader = ParseStore(kb_dir).reader(source, parsed)
    checkpoints = CompilationCheckpoints(kb_dir, source, parsed, settings, bundle)
    facts = []
    with progress_scope(
        "facts", sum(block.chars for block in parsed.blocks), "characters"
    ) as progress:
        for batch in fact_batches(
            source_units(kb_dir, source, parsed, limits, model), limits, model
        ):
            processing_checkpoint("facts")
            payload = {"stage": "facts", "units": batch}
            key = checkpoints.key(FACTS_SYSTEM, payload)
            result = checkpoints.load(key)
            on_event({"stage": "facts", "blocks": len(batch), "cached": result is not None})
            if result is None:
                result = _object(
                    _llm_call(
                        model,
                        messages(FACTS_SYSTEM, payload),
                        "facts",
                        bundle=bundle,
                        response_format=JSON_FORMAT,
                        **compilation_model_options(settings),
                    )
                )
            outputs = result.get("units")
            if not isinstance(outputs, list) or len(outputs) != len(batch):
                raise ProcessingIncomplete("section_coverage_incomplete", "facts")
            expected = {unit["id"]: unit for unit in batch}
            seen = set()
            for item in outputs:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("id"), str)
                    or item["id"] not in expected
                    or item["id"] in seen
                ):
                    raise ProcessingIncomplete("section_coverage_incomplete", "facts")
                seen.add(item["id"])
                unit = expected[item["id"]]
                extracted = item.get("facts")
                if (
                    not isinstance(extracted, list)
                    or (not extracted and not isinstance(item.get("empty_reason"), str))
                    or (not extracted and not item["empty_reason"].strip())
                ):
                    raise ProcessingIncomplete("section_empty_without_reason", "facts")
                for fact in extracted:
                    if (
                        not isinstance(fact, dict)
                        or not all(
                            isinstance(fact.get(key), str) and fact[key].strip()
                            for key in ("topic", "statement", "quote")
                        )
                        or fact["quote"] not in unit["text"]
                    ):
                        raise ProcessingIncomplete("fact_evidence_invalid", "facts")
                    reference = dict(unit["reference"])
                    reference["start"] += unit["text"].index(fact["quote"])
                    reference["end"] = reference["start"] + len(fact["quote"])
                    value = {
                        "topic": fact["topic"],
                        "statement": fact["statement"],
                        "quote": fact["quote"],
                        "reference": reference,
                        "scope": unit["reference"],
                        "context_evidence": [
                            {"reference": item["reference"], "relation": item["relation"]}
                            for item in [*unit["heading_evidence"], *unit["neighbors"]]
                        ],
                    }
                    facts.append({"id": content_id(value), **value})
            checkpoints.save(key, result)
            progress.advance(sum(len(unit["text"]) for unit in batch))
    from openkb.agent.evidence_plan import plan_topics

    wiki = workspace / "wiki"
    groups = plan_topics(
        sorted({fact["topic"] for fact in facts}),
        workspace,
        settings,
        limits,
        checkpoints,
        bundle=bundle,
        on_event=on_event,
    )
    from openkb.agent.evidence_pages import retract_retired_topics

    retract_retired_topics(
        wiki, source, f"summaries/{name}.md", {group["path"] for group in groups}
    )
    used_assets = {asset for block in parsed.blocks for asset in block.assets}
    assets = {
        path.stem: "../sources/" + directory + "/" + path.name
        for directory in ("images", "attachments")
        for path in (wiki / "sources" / directory).glob("*")
        if path.stem in used_assets and path.is_file()
    }
    if set(assets) != used_assets:
        raise ProcessingIncomplete("source_assets_incomplete", "generation")
    known_targets = (
        list_existing_wiki_targets(wiki)
        | {group["path"] for group in groups}
        | {f"summaries/{name}"}
    )
    with progress_scope("generation", len(groups), "topics") as progress:
        for group in groups:
            selected = list(
                {fact["id"]: fact for fact in facts if fact["topic"] in group["members"]}.values()
            )
            from openkb.agent.evidence_pages import generate_topic

            content = generate_topic(
                group,
                selected,
                reader,
                checkpoints,
                wiki,
                source,
                settings,
                limits,
                bundle=bundle,
                on_event=on_event,
                known_targets=known_targets,
                assets=assets,
            )
            writer = _write_entity if group["kind"] == "entity" else _write_concept
            writer(
                wiki,
                group["name"],
                content,
                f"summaries/{name}.md",
                (wiki / f"{group['path']}.md").exists(),
                brief=group["title"],
                **({"type_": group["type"]} if group["kind"] == "entity" else {}),
            )
            progress.advance()
    _write_summary(
        wiki,
        name,
        "# "
        + source.name
        + "\n\n"
        + "\n".join(f"- [[{group['path']}|{group['title']}]]" for group in groups),
    )
    _update_index(
        wiki,
        name,
        [group["name"] for group in groups if group["kind"] == "concept"],
        entity_names=[group["name"] for group in groups if group["kind"] == "entity"],
        entity_meta={
            group["name"]: (group["type"], group["title"])
            for group in groups
            if group["kind"] == "entity"
        },
    )
