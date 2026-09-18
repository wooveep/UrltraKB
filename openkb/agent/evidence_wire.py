"""Request-local identity labels; semantic strings and original positions stay exact."""

import json
import re
from collections import Counter
from types import MappingProxyType

from openkb.agent.model_json import DuplicateFieldError, json_text, unique_fields

_IDENTITY_FIELDS = {
    "start",
    "end",
    "id",
    "source_id",
    "version_id",
    "parse_id",
    "block_id",
    "fact_id",
    "block",
    "start_block",
    "toc_block",
    "index",
}
_IDENTITY_LISTS = {"covered"}


def _map(value, identities, *, field="", create=False, namespace=None):
    if isinstance(value, dict):
        return {
            key: _map(item, identities, field=key, create=create, namespace=namespace)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _map(item, identities, field=field, create=create, namespace=namespace)
            for item in value
        ]
    if isinstance(value, str) and field in _IDENTITY_FIELDS | _IDENTITY_LISTS:
        if create and re.fullmatch(r"[0-9a-f]{32}|[0-9a-f]{64}", value):
            label = f"{namespace}{len(identities) + 1}" if namespace else f"r{len(identities) + 1}"
            return identities.setdefault(value, label)
        return identities.get(value, value)
    return value


class WireMessages(list):
    """Only ordinary message rows cross transport; the inverse map stays local."""

    def __init__(self, rows, identities):
        super().__init__(rows)
        self.identities = MappingProxyType(dict(identities))
        self.inverse = MappingProxyType({value: key for key, value in identities.items()})

    def decode_response(self, raw):
        try:
            raw = json_text(raw)
            value = json.loads(raw, object_pairs_hook=unique_fields)
        except DuplicateFieldError:
            from openkb.agent.evidence_retry import ResponseIncomplete

            stage = json.loads(self[-1]["content"]).get("stage", "generation")
            reason = (
                "evidence_verification_invalid"
                if stage == "verification"
                else "evidence_output_invalid"
            )
            raise ResponseIncomplete(reason, stage) from None
        except (ValueError, TypeError):
            return raw  # The stage's bounded format-recovery policy decides what to do.
        payload = json.loads(self[-1]["content"])
        if payload.get("stage") == "generation" and (protocol := payload.get("identity_protocol")):
            _check_identity_placement(value, protocol["namespace"])
        return json.dumps(_map(value, self.inverse), ensure_ascii=False)

    def encode_response(self, raw):
        return json.dumps(
            _map(json.loads(json_text(raw), object_pairs_hook=unique_fields), self.identities),
            ensure_ascii=False,
        )


def encode_payload(payload, identity_values=()):
    namespace = None
    if payload.get("stage") == "generation":
        # A private namespace absent from the entire original input cannot be
        # confused with a real source label such as r8, even inside literal code.
        namespace = "@r:"
        original = json.dumps(payload, ensure_ascii=False)
        while namespace in original:
            namespace = namespace[:-1] + "_:"
    identities = {
        value: f"{namespace}{i}" if namespace else f"r{i}"
        for i, value in enumerate(dict.fromkeys(identity_values), 1)
    }
    wire = _map(payload, identities, create=True, namespace=namespace)
    if namespace:
        wire["identity_protocol"] = {
            "namespace": namespace,
            "instruction": (
                "Private identity markers belong only in structured identity fields and covered. "
                "Never write them in titles, headings, Markdown or quotations. They are not "
                "citations. The application attaches original source references after review."
            ),
        }
    return wire, identities


def encode_frozen_payload(payload, identity_values=()):
    """Freeze source identity/context encoding before visiting dynamic task fields."""
    keys = ("evidence",) if "evidence" in payload else ("units",) if "units" in payload else ()
    evidence = {key: payload[key] for key in keys}
    namespace = None
    if payload.get("stage") in {"generation", "verification"}:
        namespace = "@r:"
        original = json.dumps(evidence, ensure_ascii=False)
        while namespace in original:
            namespace = namespace[:-1] + "_:"
    identities = {}
    prefix = _map(evidence, identities, create=True, namespace=namespace)
    prefix = share_contexts(prefix)
    for value in identity_values:
        identities.setdefault(value, f"{namespace or 'r'}{len(identities) + 1}")
    suffix = _map(
        {key: value for key, value in payload.items() if key not in keys},
        identities,
        create=True,
        namespace=namespace,
    )
    if namespace:
        suffix["identity_protocol"] = {
            "namespace": namespace,
            "instruction": "Private identity markers belong only in structured identity fields. "
            "Never write them in titles, headings, Markdown or quotations. They are not citations.",
        }
    return {**prefix, **suffix}, identities


def _check_identity_placement(value, namespace, field=""):
    if isinstance(value, dict):
        for key, item in value.items():
            _check_identity_placement(key, namespace)
            _check_identity_placement(item, namespace, key)
    elif isinstance(value, list):
        for item in value:
            _check_identity_placement(item, namespace, field)
    elif (
        isinstance(value, str)
        and namespace in value
        and field not in _IDENTITY_FIELDS | _IDENTITY_LISTS
    ):
        from openkb.agent.evidence_retry import ResponseIncomplete

        raise ResponseIncomplete("topic_generation_incomplete", "generation")


def projected_response(value, payload):
    _, identities = encode_payload(payload)
    return _map(value, identities)


def _visit_contexts(value, fields, counts, labels, pool, *, replace=False):
    # Module-level recursion has no closure cycle retaining large context bodies
    # and serialized identity keys after the request has finished using them.
    if isinstance(value, list):
        return [
            _visit_contexts(row, fields, counts, labels, pool, replace=replace) for row in value
        ]
    if not isinstance(value, dict):
        return value
    result = {}
    for field, item in value.items():
        if field in fields:
            rows = []
            for row in item if isinstance(item, list) else [item]:
                key = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if not replace:
                    counts[key] += 1
                if replace and counts[key] > 1 and len(key) > 256:
                    label = labels.setdefault(key, f"c{len(labels) + 1}")
                    pool[label] = row
                    rows.append({"context_ref": label})
                else:
                    rows.append(row)
            result[field] = rows if isinstance(item, list) else rows[0]
        else:
            result[field] = _visit_contexts(item, fields, counts, labels, pool, replace=replace)
    return result


def share_contexts(payload, *, fields=("heading_evidence", "neighbors")):
    """Intern exact repeated context values, retaining their complete source scope."""
    counts = Counter()
    labels, pool = {}, {}
    _visit_contexts(payload, fields, counts, labels, pool)
    packed = _visit_contexts(payload, fields, counts, labels, pool, replace=True)
    if pool:
        packed["context_pool"] = pool
        packed["context_protocol"] = (
            "Each {context_ref:cN} stands for the complete, unchanged context_pool[cN] value. "
            "Expand it when reading evidence. References, locations, actors, conditions and "
            "text belong to that exact occurrence; sharing its representation does not "
            "merge source ownership. Quote actual original text, never a cN label."
        )
    return packed
