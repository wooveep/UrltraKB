"""Measured, nested work counters; elapsed time and heartbeats never advance them."""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Callable, Iterator


@dataclass(frozen=True)
class ProgressStep:
    phase: str
    completed: int = 0
    total: int | None = None
    unit: str = "items"

    def __post_init__(self):
        if self.phase not in {
            "docx",
            "pdf",
            "text",
            "image_ocr",
            "cloud_ocr",
            "facts",
            "generation",
            "parse_cache",
        } or self.unit not in {"items", "paragraphs", "pages", "lines", "characters", "topics"}:
            raise ValueError("Invalid progress scope")
        if type(self.completed) is not int or self.completed < 0:
            raise ValueError("Invalid completed work count")
        if self.total is not None and (
            type(self.total) is not int or self.total < self.completed or self.total < 0
        ):
            raise ValueError("Invalid total work count")

    @property
    def percent(self) -> int | None:
        if self.total is None:
            return None
        return self.completed * 100 // self.total if self.total else 100


def read_progress(value) -> tuple[ProgressStep, ...]:
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError("Invalid progress snapshot")
    if any(not isinstance(row, dict) for row in value):
        raise ValueError("Invalid progress entry")
    return tuple(ProgressStep(**row) for row in value)


class _Reporter:
    def __init__(self, emit: Callable[[dict], None]):
        self.emit = emit
        self.previous: tuple[ProgressStep, ...] = ()
        self.last_sent = 0.0

    def send(self, *, force=False):
        steps = tuple(counter.step for counter in _STACK.get())
        now = time.monotonic()
        changed_scope = tuple(s.phase for s in steps) != tuple(s.phase for s in self.previous)
        if steps == self.previous or (
            not force and not changed_scope and now - self.last_sent < 0.25
        ):
            return
        self.previous, self.last_sent = steps, now
        self.emit({"event": "progress", "progress": [asdict(step) for step in steps]})


_REPORTER: ContextVar[_Reporter | None] = ContextVar("work_progress_reporter", default=None)
_STACK: ContextVar[tuple[WorkCounter, ...]] = ContextVar("work_progress_stack", default=())


def _send(*, force=False):
    reporter = _REPORTER.get()
    if reporter is not None:
        reporter.send(force=force)


class WorkCounter:
    def __init__(self, phase: str, total: int | None, unit: str):
        self.step = ProgressStep(phase, 0, total, unit)

    def advance(self, count: int = 1):
        if type(count) is not int or count < 0:
            raise ValueError("Work counters cannot move backwards")
        self.step = ProgressStep(
            self.step.phase, self.step.completed + count, self.step.total, self.step.unit
        )
        _send(force=self.step.completed == self.step.total)


@contextmanager
def progress_reporting(emit: Callable[[dict], None]) -> Iterator[None]:
    """Bind application events once, preserving nested operations' outer counters."""
    if _REPORTER.get() is not None:
        yield
        return
    token = _REPORTER.set(_Reporter(emit))
    try:
        yield
    finally:
        _REPORTER.reset(token)


@contextmanager
def progress_scope(phase: str, total: int | None = None, unit: str = "items"):
    counter = WorkCounter(phase, total, unit)
    token = _STACK.set((*_STACK.get(), counter))
    _send()
    finished = False
    try:
        yield counter
        finished = True
    except BaseException:
        _send(force=True)  # Preserve the actual stopping position, without claiming completion.
        raise
    finally:
        _STACK.reset(token)
        if finished:
            _send()
