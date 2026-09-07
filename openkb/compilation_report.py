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
