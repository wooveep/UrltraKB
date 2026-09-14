"""Lossless request-local aliases for repeated original evidence identities."""

import json
import re
from collections import Counter

_IDENTITY = re.compile(r"(?<![0-9a-f])(?:[0-9a-f]{64}|[0-9a-f]{32})(?![0-9a-f])")


def _strings(value, replace):
    if isinstance(value, str):
        return replace(value)
    if isinstance(value, list):
        return [_strings(row, replace) for row in value]
    if isinstance(value, dict):
        result = {replace(key): _strings(row, replace) for key, row in value.items()}
        if len(result) != len(value):
            raise ValueError("Identity expansion produced duplicate fields")
        return result
    return value


def pack_review(payload):
    """Keep every value, position and relationship; represent repeated hashes once."""
    serialized = json.dumps(payload, ensure_ascii=False)
    identities = [
        identity for identity, count in Counter(_IDENTITY.findall(serialized)).items() if count > 1
    ]
    if not identities:
        return payload, {}
    prefix, index = "⟪id:", 1
    while prefix in serialized:
        index += 1
        prefix = f"⟪id{index}:"
    aliases = {value: f"{prefix}{i}⟫" for i, value in enumerate(identities, 1)}
    inverse = {label: value for value, label in aliases.items()}
    packed = _strings(
        payload, lambda text: _IDENTITY.sub(lambda match: aliases.get(match[0], match[0]), text)
    )
    return {
        **packed,
        "identity_pool": inverse,
        "identity_protocol": (
            "Each identity alias denotes the exact full string in identity_pool, everywhere "
            "it appears in this request, including questions, answer, citations and source "
            "observations. No source text or relationship has been omitted. Use these aliases "
            "when copying claims, quotes, paths or values; the application expands them before "
            "exact validation. A pool entry alone is not factual support: bind support to its "
            "identified observation and original context. The full original identities remain "
            "available in the pool when an identity itself is requested."
        ),
    }, inverse


def unpack_review(value, identities):
    """Restore reviewed claims and supports before locating any authorized edit."""
    if not identities:
        return value
    aliases = re.compile("|".join(re.escape(label) for label in identities))
    return _strings(value, lambda text: aliases.sub(lambda match: identities[match[0]], text))
