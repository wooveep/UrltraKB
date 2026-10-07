"""Normalize numeric version notation without inventing equivalence between versions."""

import re
import unicodedata


def canonical_version(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip()
    match = re.fullmatch(r"[vV]?\s*(\d+(?:\.\d+)*)", value)
    return match[1] if match else value


def canonical_versions(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({canonical_version(value) for value in values}))


def version_key(value: str) -> str:
    return canonical_version(value.rstrip(".")).casefold()
