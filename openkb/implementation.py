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
    # Cache identity and rejected-capacity recovery changed; successful semantic
    # requests and their rules did not. Dependency review has new inputs and
    # deliberately retains its new revision. No other revision is compatible.
    (
        "openkb.agent.evidence_verifier",
        "a6580a991ce4e18a52ed45c899515579ca9e7500e429f1de534e7b3a3b4afe5d",
    ): "44ffd30309c3d2e59ca98e9914a7b3816d2265f45f0e3695eed5791337dfe1a4",
    (
        "openkb.agent.dependency_preflight",
        "627dbfea77b49846ee2266684f4f4cc04d8c556f2b26ab5ae5bc0b122c528fe6",
    ): "c343402ad5b592accbf78edf51c7555a8d6d57bb5b73291f52072b22ebdf6101",
    (
        "openkb.agent.review_batching",
        "43d2f094537026511663a99b95dff31e0150be1cfc3e54822b69bb0116f8a9d9",
    ): "24b864886597755448e55f5e96afb0225d33d88f136471dc01314ebd9316c356",
    (
        "openkb.processing",
        "f2415ee598bc5b44effcbe4e89bd2fb6cf79deda649f9de60d80addab2313a01",
    ): "d7fccd55a14ce4e8ae8b178a30d7d78e08d39c781f9267ad4a2a5df8c5907123",
    (
        "openkb.agent.evidence_compiler",
        "15f2ecb0e3eb9d66f3b997455fcd169a03fdc6da1614d070b3c09e0b02e532b3",
    ): "72d489daf2991201682539378c9c4f2494c88a84428db2224eb9ec7f70029687",
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
