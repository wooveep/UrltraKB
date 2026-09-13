"""Exact semantic analysis inputs and independent bindings inside one knowledge base."""

import copy
import hashlib
import time

from openkb.agent.analysis_flights import LOCK as _LOCK
from openkb.execution_measurement import measurement_identity, record_analysis
from openkb.implementation import module_revision
from openkb.locks import atomic_write_bytes, atomic_write_json
from openkb.processing import processing_checkpoint, request_budget_settings
from openkb.sources import content_id, read_object

_POSITIONS = {
    "page",
    "paragraph",
    "table",
    "row",
    "cell",
    "line",
    "bbox",
    "sheet_index",
    "cell_address",
    "cell_range",
    "slide",
    "object_id",
    "coordinate_unit",
    "group_ids",
}


def semantic_location(location):
    # Preserve worksheet names, heading paths, notes and attachment identity.
    result = {key: value for key, value in location.items() if key not in _POSITIONS}
    if "attachment" in result:
        attachment = result["attachment"]
        result["attachment"] = {**attachment, "position": semantic_location(attachment["position"])}
    return result


def fact_input(unit):
    result = {
        key: value
        for key, value in unit.items()
        if key
        not in {
            "id",
            "reference",
            "span",
            "location",
            "neighbors",
            "heading_evidence",
            "navigation",
        }
    }
    result["location"] = semantic_location(unit["location"])
    result["span"] = {key: value for key, value in unit["span"].items() if key != "block"}
    for field in ("neighbors", "heading_evidence"):
        result[field] = [
            {
                key: semantic_location(value) if key == "location" else value
                for key, value in row.items()
                if key != "reference"
            }
            for row in unit[field]
        ]
    return result


class SharedAnalysis:
    def __init__(self, checkpoints, stage, system, *, options=None, rules=(), value_valid=None):
        self.cp, self.stage = checkpoints, stage
        self.value_valid = value_valid or (lambda value: True)
        self.root = checkpoints.store.owned_path(checkpoints.store.root / "analysis")
        self.contract = {
            "schema": 1,
            "stage": stage,
            "system": system,
            "capabilities": checkpoints.analysis_options,
            "options": checkpoints.input.get("model_options", {}) if options is None else options,
            "model": {
                key: value
                for key, value in checkpoints.input.items()
                if key not in {"source", "version", "parse", "model_options"}
            },
            "rules": {
                name: module_revision(name)
                for name in (
                    __name__,
                    "openkb.agent.evidence_quotes",
                    "openkb.agent.evidence_coverage",
                    "openkb.agent.evidence_wire",
                    "openkb.agent.model_json",
                    "openkb.agent.request_analysis",
                    "openkb.agent.compiler",
                    "openkb.execution_receipt",
                    "openkb.processing",
                    *(("openkb.agent.evidence_facts",) if stage == "facts" else ()),
                    *rules,
                )
            },
        }

    def output_tokens(self):
        options = self.contract["options"]
        if "max_tokens" in options or "max_completion_tokens" in options:
            return options.get("max_completion_tokens", options.get("max_tokens"))
        active = request_budget_settings()
        return active["max_tokens"] if active else self.cp.analysis_options["output_tokens"]

    def key(self, payload, *, output_tokens=None):
        return {
            **self.contract,
            "effective_output_tokens": self.output_tokens()
            if output_tokens is None
            else output_tokens,
            "payload": payload,
        }

    def input_reference(self, value):
        """Store a full batch dependency once, while each occurrence refers to its digest."""
        identity = content_id(value)
        path = self.cp.store.owned_path(self.root / "inputs" / f"{identity}.json")
        record = {"id": identity, "value": value, "digest": identity}
        with _LOCK:
            try:
                saved = read_object(path) if path.exists() else None
            except (ValueError, FileNotFoundError):
                saved = None
            if saved != record:
                atomic_write_json(path, record)
        return identity

    def load(self, payload, *, observe=True, output_tokens=None):
        started = time.monotonic()
        contract = self.key(payload, output_tokens=output_tokens)
        identity = content_id(contract)
        path = self.cp.store.owned_path(self.root / "records" / f"{identity}.json")
        with _LOCK:
            if not path.exists():
                if observe:
                    record_analysis(self.stage, "miss", identity, time.monotonic() - started)
                return None
            try:
                record = read_object(path)
                if (
                    record.get("input") != contract
                    or record.get("id") != identity
                    or record.get("digest") != content_id(record.get("value"))
                    or not self.value_valid(record.get("value"))
                ):
                    raise ValueError("Shared analysis contract or digest mismatch")
                if observe:
                    record_analysis(self.stage, "hit", identity, time.monotonic() - started)
                return copy.deepcopy(record["value"])
            except (ValueError, KeyError, FileNotFoundError):
                if observe:
                    record_analysis(
                        self.stage,
                        "invalid",
                        identity,
                        time.monotonic() - started,
                        "analysis_contract_or_digest_invalid",
                    )
                # Worker threads do not own the entrance thread's mutation lease.
                # Preserve corrupt bytes for the lease owner's cleanup; this one
                # analysis is simply unavailable and cannot authorize publication.
                return None

    def save(self, payload, value, *, output_tokens):
        started = time.monotonic()
        processing_checkpoint(self.stage)
        contract = self.key(payload, output_tokens=output_tokens)
        identity = content_id(contract)
        path = self.cp.store.owned_path(self.root / "records" / f"{identity}.json")
        with _LOCK:
            valid = self.load(payload, observe=False, output_tokens=output_tokens)
            if valid is None:
                if path.exists():
                    raw = path.read_bytes()
                    quarantine = self.cp.store.owned_path(
                        self.root / "quarantine" / f"{hashlib.sha256(raw).hexdigest()}.json"
                    )
                    # Private cache writes use the same atomic file helper as
                    # compilation checkpoints; no Wiki mutation lease is acquired.
                    atomic_write_bytes(quarantine, raw)
                atomic_write_json(
                    path,
                    {
                        "id": identity,
                        "input": contract,
                        "value": value,
                        "digest": content_id(value),
                        "producer": self.cp.input,
                        "producer_measurement": measurement_identity(),
                    },
                )
                record_analysis(self.stage, "produced", identity, time.monotonic() - started)
        return identity

    def bind(self, payload, unit, *, output_tokens=None):
        started = time.monotonic()
        identity = content_id(self.key(payload, output_tokens=output_tokens))
        record = {
            "analysis": identity,
            "source": self.cp.input,
            "unit": unit["id"],
            "reference": unit["reference"],
        }
        path = self.cp.store.owned_path(
            self.root / "bindings" / self.cp.input["version"] / f"{content_id(record)}.json"
        )
        atomic_write_json(path, record)
        record_analysis(self.stage, "binding", identity, time.monotonic() - started)

    def claim(self, payload):
        from openkb.agent.analysis_flights import claim

        with _LOCK:
            # Coordinate the complete adaptive policy. A retry at a larger cap
            # stays owned by this executor; saved results retain their actual cap.
            return claim((str(self.root), content_id({**self.contract, "payload": payload})))
