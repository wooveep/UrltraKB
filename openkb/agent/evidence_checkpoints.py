"""Validated compilation responses keyed by their actual immutable inputs."""

import threading

from openkb.config import compilation_model_options
from openkb.implementation import module_revision
from openkb.locks import atomic_write_json
from openkb.processing import processing_checkpoint
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
            "implementation": {
                name: module_revision("openkb.agent." + name)
                for name in (
                    "evidence_checkpoints",
                    "evidence_compiler",
                    "evidence_coverage",
                    "evidence_parallel",
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
        stage_modules = {
            "facts": ("evidence_compiler", "evidence_units", "evidence_retry", "evidence_coverage"),
            "planning": ("evidence_plan", "evidence_retry"),
            "generation": (
                "evidence_pages",
                "evidence_verifier",
                "evidence_markup",
                "evidence_retry",
            ),
        }
        return content_id(
            {
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
        )

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
