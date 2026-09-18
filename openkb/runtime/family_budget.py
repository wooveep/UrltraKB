"""Durable request allowance shared by a task, its attachments and retries."""

import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

from openkb.locks import atomic_write_json, file_write_lock
from openkb.processing import ProcessingIncomplete
from openkb.sources import content_id, read_object

_FAMILY = ContextVar("task_family_budget", default=None)


def bind_family(receipt_dir, task_id, related=None):
    root = receipt_dir.parent / "families"
    binding = root / "tasks" / f"{task_id}.json"
    family = task_id
    if related is not None:
        previous = root / "tasks" / f"{related}.json"
        if previous.exists():
            family = read_object(previous)["family"]
        else:
            family = related
    atomic_write_json(binding, {"family": family})


@contextmanager
def family_scope(receipt_dir, task_id):
    root = receipt_dir.parent / "families"
    binding = root / "tasks" / f"{task_id}.json"
    if not binding.exists():
        # Workers admitted before this release still have their own identity.
        bind_family(receipt_dir, task_id)
    family = read_object(binding)["family"]
    if (
        not isinstance(family, str)
        or len(family) != 32
        or any(c not in "0123456789abcdef" for c in family)
    ):
        raise ValueError("Invalid task family identity")
    token = _FAMILY.set(FamilyBudget(root / f"{family}.json"))
    try:
        yield
    finally:
        _FAMILY.reset(token)


def current_family():
    return _FAMILY.get()


def _source_binding(root, kb_dir, version_id):
    key = content_id({"kb": os.path.normcase(str(kb_dir.resolve())), "version": version_id})
    return root / "sources" / f"{key}.json"


def source_family(receipt_dir, kb_dir, requests):
    families = set()
    for request in requests:
        version = getattr(request, "version_id", None)
        if version is not None:
            path = _source_binding(receipt_dir.parent / "families", kb_dir, version)
            if path.exists():
                families.add(read_object(path)["family"])
    if len(families) > 1:
        raise ValueError("Continue separate task families in separate tasks")
    return next(iter(families), None)


def register_source_family(kb_dir, source):
    if family := current_family():
        path = _source_binding(family.path.parent, kb_dir, source.id)
        with file_write_lock(path.with_suffix(".lock")):
            if not path.exists():
                atomic_write_json(path, {"family": family.path.stem})


class FamilyBudget:
    def __init__(self, path):
        self.path = path

    def memory_limit(self, proposed):
        path = self.path.with_suffix(".resources.json")
        with file_write_lock(path.with_suffix(".lock")):
            if path.exists():
                return read_object(path)["memory_bytes"]
            atomic_write_json(path, {"memory_bytes": proposed})
            return proposed

    def reserve_ocr_page(self, limit):
        path = self.path.with_suffix(".ocr.json")
        with file_write_lock(path.with_suffix(".lock")):
            value = read_object(path) if path.exists() else {"pages": 0, "limit": limit}
            value["limit"] = min(value["limit"], limit)
            if value["pages"] >= value["limit"]:
                raise ProcessingIncomplete("ocr_page_budget_exhausted", "ocr")
            # Reserve before POST. Unknown/rejected submissions never reset the
            # family allowance; a cached downloaded page never reaches this call.
            value["pages"] += 1
            atomic_write_json(path, value)

    def reserve(self, limits, tokens, stage, timeout, *, model_time=True):
        with file_write_lock(self.path.with_suffix(".lock")):
            value = (
                read_object(self.path)
                if self.path.exists()
                else {
                    "requests": 0,
                    "tokens": 0,
                    "reservations": {},
                    "max_requests": limits.max_requests,
                    "max_tokens": limits.max_tokens,
                    "seconds": 0,
                    "max_seconds": limits.document_timeout,
                }
            )
            for name in ("max_requests", "max_tokens"):
                ceiling = getattr(limits, name)
                if ceiling is not None:
                    value[name] = min(value[name], ceiling) if value[name] is not None else ceiling
            if value["max_requests"] is not None and value["requests"] >= value["max_requests"]:
                raise ProcessingIncomplete("request_budget_exhausted", stage)
            if value["max_tokens"] is not None and value["tokens"] + tokens > value["max_tokens"]:
                raise ProcessingIncomplete("token_budget_exhausted", stage)
            if limits.document_timeout is not None:
                value["max_seconds"] = min(
                    value["max_seconds"] or limits.document_timeout, limits.document_timeout
                )
            if model_time and value["max_seconds"] is not None:
                timeout = min(timeout, value["max_seconds"] - value["seconds"])
                if timeout <= 0:
                    raise ProcessingIncomplete("time_budget_exhausted", stage)
            key = uuid4().hex
            value["requests"] += 1
            value["tokens"] += tokens
            # An interrupted/unknown request keeps its full reservation after restart.
            reserved_seconds = timeout if model_time else 0
            value["seconds"] += reserved_seconds
            value["reservations"][key] = {
                "tokens": tokens,
                "seconds": reserved_seconds,
                "model_time": model_time,
            }
            atomic_write_json(self.path, value)
            return {"key": key, "started": time.monotonic(), "timeout": timeout}

    def settle(self, key, tokens, stage):
        with file_write_lock(self.path.with_suffix(".lock")):
            value = read_object(self.path)
            reserved = value["reservations"].pop(key["key"])
            value["tokens"] += tokens - reserved["tokens"]
            if reserved.get("model_time", True):
                value["seconds"] += time.monotonic() - key["started"] - reserved["seconds"]
            atomic_write_json(self.path, value)
            if value["max_tokens"] is not None and value["tokens"] > value["max_tokens"]:
                raise ProcessingIncomplete("token_budget_exhausted", stage)
            if value["max_seconds"] is not None and value["seconds"] > value["max_seconds"]:
                raise ProcessingIncomplete("time_budget_exhausted", stage)
