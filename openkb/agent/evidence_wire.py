"""Request-local identity labels; semantic strings and original positions stay exact."""

import json
import re
from collections import Counter
from types import MappingProxyType

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
    "index",
}
_IDENTITY_LISTS = {"covered"}


def _map(value, identities, *, field="", create=False):
    if isinstance(value, dict):
        return {
            key: _map(item, identities, field=key, create=create) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_map(item, identities, field=field, create=create) for item in value]
    if isinstance(value, str) and field in _IDENTITY_FIELDS | _IDENTITY_LISTS:
        if create and re.fullmatch(r"[0-9a-f]{32}|[0-9a-f]{64}", value):
            return identities.setdefault(value, f"r{len(identities) + 1}")
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
            value = json.loads(raw)
        except (ValueError, TypeError):
            return raw  # The stage's bounded format-recovery policy decides what to do.
        return json.dumps(_map(value, self.inverse), ensure_ascii=False)

    def encode_response(self, raw):
        return json.dumps(_map(json.loads(raw), self.identities), ensure_ascii=False)


def encode_payload(payload, identity_values=()):
    identities = {value: f"r{i}" for i, value in enumerate(dict.fromkeys(identity_values), 1)}
    return _map(payload, identities, create=True), identities


def projected_response(value, payload):
    _, identities = encode_payload(payload)
    return _map(value, identities)


def share_contexts(payload, *, fields=("heading_evidence", "neighbors")):
    """Intern exact repeated context values, retaining their complete source scope."""
    counts = Counter()

    def identity(row):
        return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def visit(value, replace=False):
        if isinstance(value, list):
            return [visit(row, replace) for row in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for field, item in value.items():
            if field in fields:
                rows = []
                for row in item if isinstance(item, list) else [item]:
                    key = identity(row)
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
                result[field] = visit(item, replace)
        return result

    labels, pool = {}, {}
    visit(payload)
    packed = visit(payload, True)
    if pool:
        packed["context_pool"] = pool
        packed["context_protocol"] = (
            "Each {context_ref:cN} stands for the complete, unchanged context_pool[cN] value. "
            "Expand it when reading evidence. References, locations, actors, conditions and "
            "text belong to that exact occurrence; sharing its representation does not "
            "merge source ownership. Quote actual original text, never a cN label."
        )
    return packed
