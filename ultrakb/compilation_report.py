"""Structured compiler quality facts, independent of terminal diagnostics.

Each application call collects its own report. Async model tasks inherit the
collector; unrelated operations cannot mix their facts. Legacy callers retain
their existing return values and diagnostic output.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from typing import Iterator

from ultrakb.ingest_result import IngestResult


@dataclass
class CompileReport:
    quality: list[str] = field(default_factory=list)
    unfinished: list[str] = field(default_factory=list)
    checkpoints: list[dict] = field(default_factory=list)
    repairs: int = 0

    def require_complete(self) -> None:
        if self.unfinished:
            raise CompilationIncomplete(tuple(self.quality), tuple(self.unfinished))


@dataclass(frozen=True)
class CompileWork:
    """Mechanical work counts, independent of semantic correctness or budget."""

    stage: str
    completed: int
    pending: int
    in_flight: int = 0
    splits: int = 0
    repairs: int = 0

    def __post_init__(self):
        if (
            not isinstance(self.stage, str)
            or not self.stage
            or any(
                type(value) is not int or value < 0
                for value in (
                    self.completed,
                    self.pending,
                    self.in_flight,
                    self.splits,
                    self.repairs,
                )
            )
        ):
            raise ValueError("Invalid compilation work progress")


def report_compile_work(
    stage: str, *, completed: int, pending: int, in_flight=0, splits=0, repairs=0
):
    from ultrakb.llm_usage import active_scope

    work = CompileWork(stage, completed, pending, in_flight, splits, repairs)
    if (scope := active_scope()) and scope.on_event:
        try:
            scope.on_event({"event": "compile_work", "data": asdict(work)})
        except Exception:
            import logging

            logging.getLogger(__name__).warning(
                "Compilation work progress delivery failed", exc_info=True
            )


def merge_compile_report(result: IngestResult, report: CompileReport) -> IngestResult:
    """Deliver collected compiler facts without losing existing import diagnostics."""
    return replace(
        result,
        quality=tuple(dict.fromkeys((*result.quality, *report.quality))),
        unfinished=tuple(dict.fromkeys((*result.unfinished, *report.unfinished))),
    )


class CompilationIncomplete(RuntimeError):
    """An exhausted or incomplete compile must not publish or restart the whole pipeline."""

    def __init__(self, quality: tuple[str, ...], unfinished: tuple[str, ...], detail: str = ""):
        self.quality = quality
        self.unfinished = unfinished
        super().__init__(
            "Compilation incomplete: " + ", ".join(unfinished) + (f"; {detail}" if detail else "")
        )


_ACTIVE: ContextVar[CompileReport | None] = ContextVar("ultrakb_compile_report", default=None)


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


def report_compile_checkpoint(path: str, batch: str, state: str) -> None:
    if report := _ACTIVE.get():
        entry = {"path": path, "batch": batch, "state": state}
        if entry not in report.checkpoints:
            report.checkpoints.append(entry)


def report_compile_repair() -> None:
    if report := _ACTIVE.get():
        report.repairs += 1


def compile_repair_count() -> int:
    report = _ACTIVE.get()
    return report.repairs if report else 0


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
                parent.checkpoints.extend(report.checkpoints)
                parent.quality.extend(code for code in report.quality if code not in parent.quality)
                parent.unfinished.extend(
                    stage for stage in report.unfinished if stage not in parent.unfinished
                )
            raise
        else:
            if parent is not None:
                parent.checkpoints.extend(report.checkpoints)
                parent.quality.extend(code for code in report.quality if code not in parent.quality)
