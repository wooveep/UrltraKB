"""Validate persisted task allowance records before trusting counters or paths."""

import math

from openkb.sources import read_object


def family_id(value):
    if (
        not isinstance(value, str)
        or len(value) != 32
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError("Invalid task family identity")
    return value


def binding(path):
    value = read_object(path)
    if set(value) != {"family"}:
        raise ValueError("Invalid task family binding")
    return family_id(value["family"])


def number(value, *, integer=False, positive=False):
    if (
        type(value) not in ((int,) if integer else (int, float))
        or not math.isfinite(value)
        or (value <= 0 if positive else value < 0)
    ):
        raise ValueError("Invalid task family allowance")
    return value


def counters(path):
    value = read_object(path)
    if set(value) != {
        "requests",
        "tokens",
        "seconds",
        "reservations",
        "max_requests",
        "max_tokens",
        "max_seconds",
    }:
        raise ValueError("Invalid task family counters")
    for name in ("requests", "tokens", "seconds"):
        number(value[name], integer=name != "seconds")
        if value["max_" + name] is not None:
            number(value["max_" + name], integer=name != "seconds", positive=True)
    reservations = value["reservations"]
    if not isinstance(reservations, dict):
        raise ValueError("Invalid task family reservations")
    for key, row in reservations.items():
        family_id(key)
        if not isinstance(row, dict) or set(row) not in (
            {"tokens", "seconds"},
            {"tokens", "seconds", "model_time"},
        ):
            raise ValueError("Invalid task family reservation")
        number(row["tokens"], integer=True)
        number(row["seconds"])
        if type(row.get("model_time", True)) is not bool:
            raise ValueError("Invalid task family reservation")
        if row.get("model_time") is False and row["seconds"] != 0:
            raise ValueError("Invalid non-model reservation")
    if value["requests"] < len(reservations) or any(
        value[name] + (1e-6 if name == "seconds" else 0)
        < sum(row[name] for row in reservations.values())
        for name in ("tokens", "seconds")
    ):
        raise ValueError("Inconsistent task family reservations")
    return value


def resource_limit(path):
    value = read_object(path)
    if set(value) != {"memory_bytes"}:
        raise ValueError("Invalid task family resource limit")
    return number(value["memory_bytes"], integer=True, positive=True)


def ocr_counter(path):
    value = read_object(path)
    if set(value) != {"pages", "limit"}:
        raise ValueError("Invalid task family OCR counters")
    number(value["pages"], integer=True)
    number(value["limit"], integer=True, positive=True)
    return value
