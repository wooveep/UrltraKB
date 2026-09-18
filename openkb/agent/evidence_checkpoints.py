"""Validated compilation responses keyed by their actual immutable inputs."""

import threading
from contextlib import contextmanager
from copy import deepcopy

from openkb.agent.checkpoint_contracts import PendingContracts
from openkb.config import compilation_model_options
from openkb.evidence import EVIDENCE_PROVENANCE
from openkb.implementation import module_revision
from openkb.locks import atomic_write_json
from openkb.processing import processing_checkpoint
from openkb.resource_checks import check_disk, json_size, resource_operation
from openkb.sources import SourceStore, content_id, read_object, valid_id


def compilation_profile(settings, bundle):
    return content_id(
        {
            "settings": {
                key: value
                for key, value in settings.items()
                if key not in {"processing", "navigation", "parsing"}
            },
            "endpoint": content_id(getattr(bundle, "base_url", None)),
            "headers": content_id(getattr(bundle, "extra_headers", None)),
            "source_omissions": module_revision("openkb.source_omissions"),
            "source_coverage": module_revision("openkb.source_coverage"),
            "source_summary": module_revision("openkb.source_summary"),
            "compilation_omissions": module_revision("openkb.compilation_omissions"),
            "evidence_snapshot": module_revision("openkb.evidence_snapshot"),
            "evidence_reader": module_revision("openkb.evidence"),
            "pageindex_reader": module_revision("openkb.pageindex_evidence"),
            "pageindex_store": module_revision("openkb.pageindex_store"),
            "pageindex_bindings": module_revision("openkb.pageindex_bindings"),
            "evidence_context": module_revision("openkb.evidence_context"),
            "source_context": module_revision("openkb.source_context"),
            "implementation": {
                name: module_revision("openkb.agent." + name)
                for name in (
                    "evidence_checkpoints",
                    "evidence_compiler",
                    "evidence_facts",
                    "evidence_fact_cache",
                    "fact_resume",
                    "evidence_coverage",
                    "evidence_parallel",
                    "evidence_quotes",
                    "evidence_units",
                    "table_objects",
                    "table_recovery",
                    "evidence_dependencies",
                    "dependency_scope",
                    "dependency_preflight",
                    "dependency_sources",
                    "compilation_storage",
                    "semantic_spans",
                    "operation_context",
                    "evidence_retry",
                    "evidence_pages",
                    "evidence_topic_cache",
                    "evidence_generation_protocol",
                    "source_positions",
                    "evidence_selection",
                    "evidence_title_context",
                    "evidence_plan",
                    "planning_resume",
                    "planning_candidates",
                    "request_analysis",
                    "shared_analysis",
                    "evidence_wire",
                    "model_json",
                    "evidence_verifier",
                    "evidence_review",
                    "review_batching",
                    "review_changes",
                    "evidence_markup",
                    "compiler",
                )
            },
        }
    )


def publication_settings(settings, bundle):
    return {**settings, "_compilation_profile": compilation_profile(settings, bundle)}


class CompilationCheckpoints:
    def __init__(self, kb_dir, source, parsed, settings, bundle):
        self._write_lock = threading.RLock()
        self._contracts = PendingContracts()
        self._owners: dict[str, int] = {}
        self._storage = None
        self.invalidations = []
        from openkb.processing import RequestLimits

        limits = RequestLimits.from_config(settings)
        self.analysis_options = {
            key: getattr(limits, key)
            for key in (
                "context_tokens",
                "output_tokens",
                "max_context_tokens",
                "max_output_tokens",
            )
        }
        self.verification_options = compilation_model_options(settings, verification=True)
        self.planning_options = compilation_model_options(settings, stage="planning")
        self.correction_options = compilation_model_options(settings, stage="correction")
        self.adjudication_options = compilation_model_options(
            settings, stage="verification_adjudication"
        )
        self.adjudication_thinking = settings.get("verification_adjudication_thinking")
        self.correction_thinking = settings.get("correction_thinking")
        self.stage_efforts = {
            key: settings[key]
            for key in ("verification_adjudication_reasoning_effort", "correction_reasoning_effort")
            if settings.get(key) is not None
        }
        self.store = SourceStore(kb_dir)
        self.root = self.store.owned_path(self.store.root / "compilation")
        self.input = {
            "source": source.source_id,
            "version": source.id,
            "parse": parsed.id,
            "compiler": "source-evidence-v1",
            "model": settings["model"],
            "model_options": compilation_model_options(settings),
            "endpoint": content_id(getattr(bundle, "base_url", None)),
            "headers": content_id(getattr(bundle, "extra_headers", None)),
        }
        self.latest = self.store.owned_path(self.root / "latest" / f"{source.id}.json")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        with self._write_lock:
            if self._owners:
                raise RuntimeError("Cannot close checkpoints with active requests")
            self._contracts.close()
            if self._storage is not None:
                self._storage.close()

    def private_rows(self, name):
        from openkb.agent.compilation_storage import CompilationStorage

        with self._write_lock:
            if self._storage is None:
                self._storage = CompilationStorage()
            return self._storage.rows(name)

    def identity(self, system, payload, *, dependencies=None):
        """Computing a cache or split identity never allocates temporary input."""
        return content_id(self._key_record(system, payload, dependencies=dependencies))

    @contextmanager
    def request(self, system, payload, *, dependencies=None):
        """Keep a contract through cache adoption, migration and durable save."""
        with self._write_lock:
            key = self.key(system, payload, dependencies=dependencies)
            self._owners[key] = self._owners.get(key, 0) + 1
        try:
            yield key
        finally:
            with self._write_lock:
                self._owners[key] -= 1
                if not self._owners[key]:
                    del self._owners[key]
                    self._contracts.discard(key)

    def key(self, system, payload, *, dependencies=None):
        record = self._key_record(system, payload, dependencies=dependencies)
        key = content_id(record)
        with self._write_lock:
            self._contracts.save(key, record)
        return key

    def previous_fact_key(self, system, payload):
        from openkb.agent.legacy_checkpoints import fact_keys

        return (
            fact_keys(self._key_record(system, payload))[0]
            if payload.get("stage") == "facts"
            else None
        )

    def review_key(self, request, model, options, attempt):
        """Raw responses are request records, not verified publication receipts.

        Keep parser revisions outside this identity so the same response can be
        revalidated after a format repair instead of rerolling a known rejection.
        """
        return content_id(
            {
                "input": self.input,
                "request": request,
                "model": model,
                "options": options,
                "request_policy": self.analysis_options,
                "attempt": attempt,
                "protocol": "verification-response-v1",
                "transport": module_revision("openkb.agent.compiler"),
            }
        )

    def previous_plan_key(self, system, payload, *, dependencies=None):
        from openkb.agent.legacy_checkpoints import plan_keys

        record = self._key_record(system, payload, dependencies=dependencies)
        return plan_keys(record)[0] if payload.get("stage") == "planning" else None

    def preceding_plan_key(self, system, payload, *, dependencies=None):
        """Same semantic contract before incremental progress; revalidate on use."""
        record = self._key_record(system, payload, dependencies=dependencies)
        record["stage_implementation"]["evidence_plan"] = (
            "fc602bbd9c064cc294ddffd20c1d54ee278eabee51a0af32e739b1594c422d87"
        )
        record["stage_implementation"].pop("planning_resume")
        return content_id(record)

    def _key_record(self, system, payload, *, dependencies=None):
        stage_modules = {
            "facts": (
                "evidence_facts",
                "evidence_fact_cache",
                "fact_resume",
                "evidence_units",
                "table_objects",
                "table_recovery",
                "evidence_retry",
                "evidence_coverage",
                "evidence_quotes",
                "semantic_spans",
            ),
            "planning": ("evidence_plan", "planning_resume", "evidence_retry"),
            "generation": (
                "evidence_pages",
                "table_objects",
                "evidence_generation_protocol",
                "source_positions",
                "evidence_selection",
                "evidence_wire",
                "evidence_title_context",
                "evidence_verifier",
                "evidence_review",
                "review_batching",
                "review_changes",
                "operation_context",
                "semantic_spans",
                "dependency_preflight",
                "dependency_scope",
                "evidence_markup",
                "evidence_retry",
            ),
        }
        return {
            "input": self.input,
            "system": system,
            "payload": payload,
            "dependencies": dependencies,
            **(
                {
                    "evidence_provenance": deepcopy(EVIDENCE_PROVENANCE),
                    "evidence_reader": module_revision("openkb.evidence"),
                }
                if payload.get("stage") == "facts"
                else {}
            ),
            # Completed source facts remain valid when only future batch limits
            # change. New shared requests have the stricter actual-dispatch key.
            **(
                {"request_policy": self.analysis_options} if payload.get("stage") != "facts" else {}
            ),
            **(
                {"planning_options": self.planning_options}
                if payload.get("stage") == "planning"
                and self.planning_options != self.input["model_options"]
                else {}
            ),
            **(
                {
                    "verification_options": self.verification_options,
                    "correction_options": self.correction_options,
                    "adjudication_options": self.adjudication_options,
                    "adjudication_thinking": self.adjudication_thinking,
                    **({"stage_efforts": self.stage_efforts} if self.stage_efforts else {}),
                    **(
                        {"correction_thinking": self.correction_thinking}
                        if self.correction_thinking is not None
                        else {}
                    ),
                    "evidence_snapshot": module_revision("openkb.evidence_snapshot"),
                    "evidence_reader": module_revision("openkb.evidence"),
                    "evidence_context": module_revision("openkb.evidence_context"),
                }
                if payload.get("stage") == "generation"
                else {}
            ),
            "implementation": module_revision("openkb.agent.compiler"),
            "message_format": module_revision("openkb.agent.evidence_units"),
            "source_context": module_revision("openkb.source_context"),
            "model_json": module_revision("openkb.agent.model_json"),
            "stage_implementation": {
                name: module_revision("openkb.agent." + name)
                for name in stage_modules.get(payload.get("stage"), ())
            },
        }

    def record(self, key):
        processing_checkpoint()
        path = self.store.owned_path(self.root / f"{valid_id(key)}.json")
        if not path.exists():
            return None
        try:
            record = read_object(path)
            if (
                record.get("input") != self.input
                or record.get("key") != key
                or content_id(record.get("value")) != record.get("value_digest")
                or (
                    "contract" in record
                    and (
                        not isinstance(record["contract"], dict)
                        or not isinstance(record["contract"].get("payload"), dict)
                        or not isinstance(record["contract"]["payload"].get("stage"), str)
                        or content_id(record["contract"]) != key
                    )
                )
            ):
                raise ValueError("Compilation checkpoint identity mismatch")
        except (ValueError, FileNotFoundError):
            self.invalidations.append({"key": key, "reason": "checkpoint_invalid"})
            return None
        # Each read already owns its decoded value. Retaining whole contracts
        # here makes a resumed long document accumulate every previous prompt.
        return record

    def load(self, key):
        record = self.record(key)
        return record["value"] if record else None

    def checkpoint_keys(self, stage):
        keys, stages = self._checkpoint_index()
        if stage in stages:
            return list(dict.fromkeys([*stages[stage], *stages.get("unknown", [])]))
        return keys

    def _checkpoint_index(self):
        try:
            index = read_object(self.latest)
            keys = index["checkpoints"]
            if not isinstance(keys, list):
                raise ValueError("Invalid checkpoint index")
            keys = [valid_id(key) for key in keys]
            known = set(keys)
            stages = index.get("stages", {})
            if not isinstance(stages, dict):
                raise ValueError("Invalid checkpoint stages")
            for name, selected in stages.items():
                if not isinstance(name, str) or not isinstance(selected, list):
                    raise ValueError("Invalid checkpoint stage")
                if not {valid_id(key) for key in selected} <= known:
                    raise ValueError("Unknown checkpoint in stage index")
            indexed = {key for selected in stages.values() for key in selected}
            if missing := known - indexed:
                stages["unknown"] = sorted(set(stages.get("unknown", [])) | missing)
            return keys, stages
        except (ValueError, KeyError, FileNotFoundError):
            self.invalidations.append({"reason": "checkpoint_index_rebuilt"})
            keys = []
            stages = {}
            for path in self.root.glob("*.json"):
                try:
                    valid_id(path.stem)
                except ValueError:
                    continue
                record = self.record(path.stem)
                if record is not None:
                    keys.append(path.stem)
                    stage = record.get("contract", {}).get("payload", {}).get("stage")
                    stages.setdefault(stage or "unknown", []).append(path.stem)
            keys.sort()
            atomic_write_json(self.latest, {"checkpoints": keys, "stages": stages})
            return keys, stages

    def save(self, key, value, *, receipt=None):
        with self._write_lock:
            with resource_operation("checkpoint", "save_completed"):
                self._save(key, value, receipt=receipt)

    def load_recovery(self, key, kind):
        """Mutable workflow state is never treated as a verified model result."""
        processing_checkpoint()
        if kind not in {"split", "draft", "review", "plan", "topic"}:
            raise ValueError("Invalid recovery checkpoint kind")
        path = self.store.owned_path(self.root / "recovery" / f"{valid_id(key)}-{kind}.json")
        if not path.exists():
            contract = self._contracts.read(key)
            if kind == "draft" and contract and contract["payload"].get("stage") == "generation":
                from openkb.agent.legacy_checkpoints import generation_keys

                for previous in generation_keys(contract):
                    old = self.store.owned_path(self.root / "recovery" / f"{previous}-draft.json")
                    if old.exists():
                        recovered = self.load_recovery(previous, kind)
                        if isinstance(recovered, dict) and isinstance(
                            recovered.get("output"), dict
                        ):
                            recovered["output"].pop("_verification", None)
                        return recovered
                    value = self.load(previous)
                    if value is not None:
                        value.pop("_verification", None)
                        return {"output": value, "revision": None, "correction": 0}
            return None
        try:
            record = read_object(path)
            if (
                record.get("input") != self.input
                or record.get("key") != key
                or record.get("kind") != kind
                or content_id(record.get("value")) != record.get("digest")
            ):
                raise ValueError("Recovery checkpoint identity mismatch")
        except (ValueError, FileNotFoundError):
            self.invalidations.append({"key": key, "reason": "recovery_invalid", "kind": kind})
            return None
        return record["value"]

    def save_recovery(self, key, kind, value):
        processing_checkpoint()
        if kind not in {"split", "draft", "review", "plan", "topic"}:
            raise ValueError("Invalid recovery checkpoint kind")
        path = self.store.owned_path(self.root / "recovery" / f"{valid_id(key)}-{kind}.json")
        check_disk(path, json_size(value), stage="checkpoint", operation="save_" + kind)
        with self._write_lock:
            atomic_write_json(
                path,
                {
                    "input": self.input,
                    "key": key,
                    "kind": kind,
                    "value": value,
                    "digest": content_id(value),
                },
            )

    def _save(self, key, value, *, receipt=None):
        processing_checkpoint()
        contract = self._contracts.read(key)
        record = {
            **({"contract": contract} if contract is not None else {}),
            "input": self.input,
            "key": key,
            "value": value,
            "value_digest": content_id(value),
            **(
                {"dispatch_output_tokens": receipt.output_tokens}
                if getattr(receipt, "output_tokens", None) is not None
                else {}
            ),
        }
        path = self.store.owned_path(self.root / f"{valid_id(key)}.json")
        check_disk(path, json_size(record), stage="checkpoint", operation="save_completed")
        if path.exists():
            prior = self.record(key)
            if prior is not None and prior["value"] != value:
                raise ValueError("Immutable compilation checkpoint changed")
            if prior is not None:
                record = prior
            if prior is None:
                import hashlib

                from openkb.locks import atomic_write_bytes

                raw = path.read_bytes()
                quarantine = self.store.owned_path(
                    self.root / "quarantine" / f"{hashlib.sha256(raw).hexdigest()}.json"
                )
                atomic_write_bytes(quarantine, raw)
                atomic_write_json(path, record)
        else:
            atomic_write_json(path, record)
        keys, stages = self._checkpoint_index() if self.latest.exists() else ([], {})
        if key not in keys:
            keys = sorted([*keys, key])
        # The validated compact index already classifies previous requests.
        # Appending one result must not decode/copy all previous request bodies.
        stages = {name: [item for item in items if item != key] for name, items in stages.items()}
        stage = record.get("contract", {}).get("payload", {}).get("stage")
        stages.setdefault(stage or "unknown", []).append(key)
        atomic_write_json(self.latest, {"checkpoints": keys, "stages": stages})
        if key not in self._owners:
            self._contracts.discard(key)
