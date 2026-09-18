"""Portable code identities for persisted results, including frozen Python distributions."""

from functools import lru_cache
from importlib.util import find_spec
from types import CodeType

from openkb.sources import content_id

# Exact wire-output equivalence reviewed for the context-body lifecycle repair.
# The wire payload, identities and semantic rules are unchanged. Preserve the
# previous contracts (including negative reviews); any other revision misses.
_COMPATIBLE_REVISIONS = {
    (
        "openkb.agent.evidence_wire",
        "d7fc50f0c5674415a9906b81d64dd67590f6f71a1514f9f3de7c1d0ddfb63c14",
    ): "45d9f407d6fc96b4cac7fb6a77b601c440cdf1cf2101b762c1a8038eef09e1d6",
}


def _code_value(value):
    if isinstance(value, CodeType):
        return {
            "instructions": value.co_code.hex(),
            "exceptions": getattr(value, "co_exceptiontable", b"").hex(),
            "constants": [_code_value(item) for item in value.co_consts],
            "names": value.co_names,
            "variables": value.co_varnames,
            "free": value.co_freevars,
            "cells": value.co_cellvars,
            "flags": value.co_flags,
            "arguments": value.co_argcount,
            "positional": value.co_posonlyargcount,
            "keywords": value.co_kwonlyargcount,
        }
    if isinstance(value, (tuple, frozenset)):
        return (
            [_code_value(item) for item in sorted(value, key=repr)]
            if isinstance(value, frozenset)
            else [_code_value(item) for item in value]
        )
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


@lru_cache(maxsize=32)
def module_revision(name: str) -> str:
    spec = find_spec(name)
    loader = spec.loader if spec else None
    get_code = getattr(loader, "get_code", None)
    code = get_code(name) if get_code else None
    if not isinstance(code, CodeType):
        raise ValueError("Cannot identify the implementation of a persisted stage")
    revision = content_id(_code_value(code))
    return _COMPATIBLE_REVISIONS.get((name, revision), revision)
