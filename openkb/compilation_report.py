"""Structured compiler quality facts, independent of terminal diagnostics.

Each application call collects its own report. Async model tasks inherit the
collector; unrelated operations cannot mix their facts. Legacy callers retain
their existing return values and diagnostic output.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class CompileReport:
    quality: list[str] = field(default_factory=list)
    unfinished: list[str] = field(default_factory=list)

    def require_complete(self) -> None:
        if self.unfinished:
            raise CompilationIncomplete(tuple(self.quality), tuple(self.unfinished))


class CompilationIncomplete(RuntimeError):
    """An exhausted or incomplete compile must not publish or restart the whole pipeline."""

    def __init__(self, quality: tuple[str, ...], unfinished: tuple[str, ...], detail: str = ""):
        self.quality = quality
        self.unfinished = unfinished
        super().__init__(
            "Compilation incomplete: " + ", ".join(unfinished) + (f"; {detail}" if detail else "")
        )


_ACTIVE: ContextVar[CompileReport | None] = ContextVar("openkb_compile_report", default=None)


@contextmanager
def collect_compile_report() -> Iterator[CompileReport]:
    report = CompileReport()
    token = _ACTIVE.set(report)
    try:
        yield report
    finally:
        _ACTIVE.reset(token)


def report_compile_issue(code: str, *unfinished: str) -> None:
    report = _ACTIVE.get()
    if report is not None:
        if code not in report.quality:
            report.quality.append(code)
        for stage in unfinished:
            if stage not in report.unfinished:
                report.unfinished.append(stage)


@contextmanager
def compilation_attempt() -> Iterator[CompileReport]:
    """Isolate an attempt; only its final outcome is merged into the enclosing receipt."""
    parent = _ACTIVE.get()
    with collect_compile_report() as report:
        try:
            yield report
            report.require_complete()
        except CompilationIncomplete:
            if parent is not None:
                parent.quality.extend(code for code in report.quality if code not in parent.quality)
                parent.unfinished.extend(
                    stage for stage in report.unfinished if stage not in parent.unfinished
                )
            raise
        else:
            if parent is not None:
                parent.quality.extend(code for code in report.quality if code not in parent.quality)
