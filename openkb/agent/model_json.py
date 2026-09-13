"""Lossless model JSON envelope handling; never infer missing fields or verdicts."""

import re


class DuplicateFieldError(ValueError):
    pass


def unique_fields(pairs):
    value = {}
    for name, item in pairs:
        if name in value:
            raise DuplicateFieldError("Duplicate model response field")
        value[name] = item
    return value


def json_text(raw):
    text = raw.strip().removeprefix("\ufeff").strip()
    wrapped = re.fullmatch(r"```(?:json)?\s*\n([\s\S]*?)\n```", text, re.IGNORECASE)
    return wrapped[1].strip() if wrapped else text
