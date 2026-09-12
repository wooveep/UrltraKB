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
_PREVIOUS_PLAN = {
    "evidence_plan": "43d41f0a8123455b9ba51a5005cda816685299c5d3524f69b0baceb956065115",
    "evidence_retry": "5b3f7fd79ecf6e56c2d5de6d946eb0a70ca4f34d96d0d3b4bdf605b72cedc199",
}
_SHORT_ID_PLAN = "cb623197965ad42025be0ab4cffa064d74474f78216869ff7876cc38d2d72ac1"
_OMISSION_PLAN = "b5f05d6f8d3eb321b2b9fc97688dcf71cb7f1396eaf7fdddc89ac89cfc40fcb2"
_PREVIOUS_GENERATION = {
    "evidence_pages": "e2b5a822a797991c51d193a93886a5ff20e5e3be4116adbb80d34f527f446068",
    "evidence_generation_protocol": (
        "cea8ee3a2b1ae480487437d4cff41c898b1168cffc11f0d7c80db63f1b9711e1"
    ),
    "evidence_verifier": "c50119c0723aa91896ce2fbe24f5c23f7d81fdbdd0a477e26366bfbe824e06c1",
    "evidence_markup": "cfbddd0a68e7346b1ed7e4d42a5f401102dbda36a4e9f4739a8fce659c6924a9",
    "evidence_retry": "5b3f7fd79ecf6e56c2d5de6d946eb0a70ca4f34d96d0d3b4bdf605b72cedc199",
}
_RECOVER_INVALID_REVIEW = "f854f09cdf7c019455af8c4581f5947869b04a961d3104fb6829f1cd2def2ff3"
_RECORDED_REVIEWS = {
    **_PREVIOUS_GENERATION,
    "evidence_pages": "74bf011eed038c022e4f08cb9a293891bfa7dc1b46646fd34147b482efb87581",
    "evidence_verifier": "850a2658d8815a8db92c2c920f65100125a7a329dbdbe6ecdcc19b82d66d8ea6",
}

_CONTEXT_GENERATION = {
    **_RECORDED_REVIEWS,
    "evidence_pages": "3b5ada95a4ce3afb58b65e1c677a780ad60ac69e27791a0ed0f44a38e4c91cf9",
}
_CORRECTION_GENERATION = {
    **_CONTEXT_GENERATION,
    "evidence_pages": "e76e91fa2359235f6c402c480fe584e8608ee4d91e29b621d1a98e369a22de60",
    "evidence_generation_protocol": (
        "a29d4cbd170c75e38898613b71cffaf4ce835fd14409ef58204710ad25c67242"
    ),
}
_DEEP_CORRECTION_GENERATION = {
    **_CORRECTION_GENERATION,
    "evidence_pages": "57553b054135ac16a97bec4d1e7f4a703596b500d38d3bb9a815f892ec60d3fa",
    "evidence_generation_protocol": (
        "56c64ec66b95fa943446a6190b99625749d11c9eccd1a77c88c516553ad83b24"
    ),
}
_TITLE_CORRECTION_GENERATION = {
    **_DEEP_CORRECTION_GENERATION,
    "evidence_pages": "a7ec20bf1ab4a054ac114fda98430ef17068def1cf47a75c9240e7e5d68f3b38",
    "evidence_generation_protocol": (
        "7ba61c21364ba923d0a16f72681e8ac9ed95627282d483cb359ffcef32f28d19"
    ),
}
_SHARED_TITLE_GENERATION = {
    **_TITLE_CORRECTION_GENERATION,
    "evidence_pages": "5adf2cc44e043258ff599c7afef9faea4fabe2c8474fa4ff502bac91f030f51a",
    "evidence_generation_protocol": (
        "ed55c769c45bd5a2eb1a568dd8e98f6890e9251926c3d78fa98e4408c6800791"
    ),
    "evidence_verifier": "e8096ba3a99472e95df9771369453340bd0f73553445498d687b35fe7aa8c504",
    "evidence_title_context": "c8c58b36fc6ed08ddeff7cced775082a4ffd55dd941cacac533760f3ce7ae135",
}
_BODY_TITLE_ISOLATION_GENERATION = {
    **_SHARED_TITLE_GENERATION,
    "evidence_generation_protocol": (
        "3c190a6f84ab84bc7a708b868184d6c442c78239861bc5508d86421808d044f7"
    ),
    "evidence_verifier": "4dc39b34c13ba185f4a214921fb0c2a2069972d88189c8174a9c0597df0ec06c",
}
_SPLIT_REVIEW_GENERATION = {
    **_BODY_TITLE_ISOLATION_GENERATION,
    "evidence_verifier": "acc66c953bb0a28687d33840fefd607798ef98d3ad96b485fc12c575ad01ce7c",
}
_OMISSION_GENERATION = {
    **_SPLIT_REVIEW_GENERATION,
    "evidence_markup": "5c3d13a443c91459403ab03a058f004e6ccc9c735d89ff50cfc0726041f7dd30",
}
_CONTEXT_READERS = {
    "evidence_context": "afc0acb56f43dc0d8596fae861a9bec3890fd8daae685d54f473a519a20b021c",
    "evidence_reader": "40a3c5017242984059acbf7bf8398f5c96041c865c1cd12a748a1ea9aaf6f6fc",
    "evidence_snapshot": "0eec4b285bd170c762d820e949753c83d86f3fff06b4f19c7a26b7ab7fb99172",
}
_PREVIOUS_READERS = {
    "evidence_reader": "d7bb8fb04bd84de3e16b3c8699069a5ec7153cf2229c646bc2dbb1a03909e03b",
    "evidence_snapshot": "e3e4446dd5c792f5c48d55bd7960b6050d97edc3bcb45b5d9ea77645bbe0d88a",
}


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
            "source_summary": module_revision("openkb.source_summary"),
            "compilation_omissions": module_revision("openkb.compilation_omissions"),
            "evidence_snapshot": module_revision("openkb.evidence_snapshot"),
            "evidence_reader": module_revision("openkb.evidence"),
            "evidence_context": module_revision("openkb.evidence_context"),
            "implementation": {
                name: module_revision("openkb.agent." + name)
                for name in (
                    "evidence_checkpoints",
                    "evidence_compiler",
                    "evidence_facts",
                    "evidence_fact_cache",
                    "evidence_coverage",
                    "evidence_parallel",
                    "evidence_quotes",
                    "evidence_units",
                    "evidence_retry",
                    "evidence_pages",
                    "evidence_topic_cache",
                    "evidence_generation_protocol",
                    "evidence_title_context",
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
        self._previous_generation_keys = {}
        self.verification_options = compilation_model_options(settings, verification=True)
        self.adjudication_thinking = settings.get("verification_adjudication_thinking")
        self.correction_thinking = settings.get("correction_thinking")
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
        record = self._key_record(system, payload, dependencies=dependencies)
        key = content_id(record)
        if record["stage_implementation"] == {
            "evidence_plan": _OMISSION_PLAN,
            "evidence_retry": _PREVIOUS_PLAN["evidence_retry"],
        }:
            prior = {
                **record,
                "stage_implementation": {
                    "evidence_plan": _SHORT_ID_PLAN,
                    "evidence_retry": _PREVIOUS_PLAN["evidence_retry"],
                },
            }
            with self._write_lock:
                self._previous_generation_keys[key] = content_id(prior)
        # The exact bounded-context change preserves old receipts only for an
        # identical payload. A newly expanded passage necessarily gets a new
        # key and review. Never bridge the discarded R9 generation contract.
        if (
            record["stage_implementation"] == _OMISSION_GENERATION
            and all(record.get(name) == revision for name, revision in _CONTEXT_READERS.items())
            and record["implementation"] == _PREVIOUS_COMPILER
            and record["message_format"] == _PREVIOUS_FACTS["evidence_units"]
        ):
            body_key = content_id(
                {**record, "stage_implementation": _BODY_TITLE_ISOLATION_GENERATION}
            )
            shared_key = content_id({**record, "stage_implementation": _SHARED_TITLE_GENERATION})
            title_key = content_id({**record, "stage_implementation": _TITLE_CORRECTION_GENERATION})
            deep_key = content_id({**record, "stage_implementation": _DEEP_CORRECTION_GENERATION})
            previous_record = dict(record)
            previous_record.pop("correction_thinking", None)
            correction_key = content_id(
                {**previous_record, "stage_implementation": _CORRECTION_GENERATION}
            )
            context_key = content_id(
                {**previous_record, "stage_implementation": _CONTEXT_GENERATION}
            )
            prior = {**previous_record, **_PREVIOUS_READERS}
            prior.pop("evidence_context")
            revisions = (
                _RECORDED_REVIEWS,
                {**_PREVIOUS_GENERATION, "evidence_verifier": _RECOVER_INVALID_REVIEW},
                _PREVIOUS_GENERATION,
            )
            with self._write_lock:
                split_key = content_id({**record, "stage_implementation": _SPLIT_REVIEW_GENERATION})
                self._previous_generation_keys[key] = split_key
                self._previous_generation_keys[split_key] = body_key
                self._previous_generation_keys[body_key] = shared_key
                self._previous_generation_keys[shared_key] = title_key
                self._previous_generation_keys[title_key] = deep_key
                self._previous_generation_keys[deep_key] = correction_key
                self._previous_generation_keys[correction_key] = context_key
                previous = context_key
                for stage in revisions:
                    old = content_id({**prior, "stage_implementation": stage})
                    self._previous_generation_keys[previous] = old
                    previous = old
        return key

    def previous_fact_key(self, system, payload):
        """Only bridge the known strict-quotation contract; the caller revalidates."""
        record = self._key_record(system, payload)
        if payload.get("stage") != "facts" or record["implementation"] != _PREVIOUS_COMPILER:
            return None
        if record["message_format"] != _PREVIOUS_FACTS["evidence_units"]:
            return None
        record["stage_implementation"] = _PREVIOUS_FACTS
        return content_id(record)

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
                "attempt": attempt,
                "protocol": "verification-response-v1",
                "transport": module_revision("openkb.agent.compiler"),
            }
        )

    def previous_plan_key(self, system, payload, *, dependencies=None):
        """Bridge only the exact functional planner before short wire identities.

        Canonical labels, prompt, model, source and catalogue dependencies stay
        identical. The planner revalidates all cached memberships before saving.
        """
        record = self._key_record(system, payload, dependencies=dependencies)
        if (
            payload.get("stage") != "planning"
            or record["implementation"] != _PREVIOUS_COMPILER
            or record["message_format"] != _PREVIOUS_FACTS["evidence_units"]
            or record["stage_implementation"].get("evidence_plan") != _OMISSION_PLAN
            or record["stage_implementation"].get("evidence_retry")
            != _PREVIOUS_PLAN["evidence_retry"]
        ):
            return None
        record["stage_implementation"] = _PREVIOUS_PLAN
        return content_id(record)

    def _key_record(self, system, payload, *, dependencies=None):
        stage_modules = {
            "facts": (
                "evidence_facts",
                "evidence_fact_cache",
                "evidence_units",
                "evidence_retry",
                "evidence_coverage",
                "evidence_quotes",
            ),
            "planning": ("evidence_plan", "evidence_retry"),
            "generation": (
                "evidence_pages",
                "evidence_generation_protocol",
                "evidence_title_context",
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
                {
                    "verification_options": self.verification_options,
                    "adjudication_thinking": self.adjudication_thinking,
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
            "stage_implementation": {
                name: module_revision("openkb.agent." + name)
                for name in stage_modules.get(payload.get("stage"), ())
            },
        }

    def load(self, key):
        processing_checkpoint()
        path = self.store.owned_path(self.root / f"{valid_id(key)}.json")
        if not path.exists():
            previous = self._previous_generation_keys.get(key)
            if previous:
                return self.load(previous)
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

    def load_recovery(self, key, kind):
        """Mutable workflow state is never treated as a verified model result."""
        processing_checkpoint()
        if kind not in {"split", "draft", "review"}:
            raise ValueError("Invalid recovery checkpoint kind")
        path = self.store.owned_path(self.root / "recovery" / f"{valid_id(key)}-{kind}.json")
        if not path.exists():
            previous = self._previous_generation_keys.get(key)
            if previous:
                return self.load_recovery(previous, kind)
            return None
        record = read_object(path)
        if (
            record.get("input") != self.input
            or record.get("key") != key
            or record.get("kind") != kind
            or content_id(record.get("value")) != record.get("digest")
        ):
            raise ValueError("Recovery checkpoint identity mismatch")
        return record["value"]

    def save_recovery(self, key, kind, value):
        processing_checkpoint()
        if kind not in {"split", "draft", "review"}:
            raise ValueError("Invalid recovery checkpoint kind")
        path = self.store.owned_path(self.root / "recovery" / f"{valid_id(key)}-{kind}.json")
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
