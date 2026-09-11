"""Validated compilation responses keyed by their actual immutable inputs."""

import threading

from openkb.config import compilation_model_options
from openkb.implementation import module_revision
from openkb.locks import atomic_write_json
from openkb.processing import processing_checkpoint
from openkb.sources import SourceStore, content_id, read_object, valid_id

# Facts from the preceding strict-quotation extractor remain usable after full
# current validation. Pin its complete executable contract; do not reuse results
# by unit ID alone or admit arbitrary older prompts/validators.
_PREVIOUS_FACTS = {
    "evidence_compiler": "7e84bc184c96861287f21855197af0929d20447b77f31898c62147d10d302aad",
    "evidence_units": "11b13587a64c24ed430ae1786bc805491b9b35952595126e1662dc267d9a42fe",
    "evidence_retry": "51ed29d4e493c8ebec15c71f0479a5b50067eddf1e472cfc0849d8908c07d476",
    "evidence_coverage": "41d9efc8652192d5f26802165496c015f15b7bc5d9edef4e057291ce694e4885",
}
_PREVIOUS_COMPILER = "833ae7bc677a7b004b141e689a8ca67974aa39584da2e2791101eacef87f6e27"


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
            "implementation": {
                name: module_revision("openkb.agent." + name)
                for name in (
                    "evidence_checkpoints",
                    "evidence_compiler",
                    "evidence_coverage",
                    "evidence_parallel",
                    "evidence_quotes",
                    "evidence_units",
                    "evidence_retry",
                    "evidence_pages",
                    "evidence_plan",
                    "evidence_verifier",
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
        self.verification_options = compilation_model_options(settings, verification=True)
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

    def key(self, system, payload, *, dependencies=None):
        return content_id(self._key_record(system, payload, dependencies=dependencies))

    def previous_fact_key(self, system, payload):
        """Only bridge the known strict-quotation contract; the caller revalidates."""
        record = self._key_record(system, payload)
        if payload.get("stage") != "facts" or record["implementation"] != _PREVIOUS_COMPILER:
            return None
        if any(
            module_revision("openkb.agent." + name) != revision
            for name, revision in _PREVIOUS_FACTS.items()
            if name != "evidence_compiler"
        ):
            return None
        record["stage_implementation"] = _PREVIOUS_FACTS
        return content_id(record)

    def _key_record(self, system, payload, *, dependencies=None):
        stage_modules = {
            "facts": (
                "evidence_compiler",
                "evidence_units",
                "evidence_retry",
                "evidence_coverage",
                "evidence_quotes",
            ),
            "planning": ("evidence_plan", "evidence_retry"),
            "generation": (
                "evidence_pages",
                "evidence_verifier",
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
                {"verification_options": self.verification_options}
                if payload.get("stage") == "generation"
                else {}
            ),
            "implementation": module_revision("openkb.agent.compiler"),
            "message_format": module_revision("openkb.agent.evidence_units"),
            "stage_implementation": {
                name: module_revision("openkb.agent." + name)
                for name in stage_modules.get(payload.get("stage"), ())
            },
        }

    def load(self, key):
        processing_checkpoint()
        path = self.store.owned_path(self.root / f"{valid_id(key)}.json")
        if not path.exists():
            return None
        record = read_object(path)
        if (
            record.get("input") != self.input
            or record.get("key") != key
            or content_id(record.get("value")) != record.get("value_digest")
        ):
            raise ValueError("Compilation checkpoint identity mismatch")
        return record["value"]

    def save(self, key, value):
        with self._write_lock:
            self._save(key, value)

    def _save(self, key, value):
        processing_checkpoint()
        record = {
            "input": self.input,
            "key": key,
            "value": value,
            "value_digest": content_id(value),
        }
        path = self.store.owned_path(self.root / f"{valid_id(key)}.json")
        if path.exists():
            if read_object(path) != record:
                raise ValueError("Immutable compilation checkpoint changed")
        else:
            atomic_write_json(path, record)
        keys = read_object(self.latest)["checkpoints"] if self.latest.exists() else []
        if key not in keys:
            atomic_write_json(self.latest, {"checkpoints": sorted([*keys, key])})
