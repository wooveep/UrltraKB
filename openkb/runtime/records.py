"""Durable task summaries and per-unit receipts, without replayable requests."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from openkb.application.pages import Page
from openkb.locks import atomic_write_json
from openkb.runtime.requests import UnitRequest

PROTOCOL_VERSION = 1
TERMINAL = frozenset({"completed", "partial", "failed", "stopped", "interrupted", "blocked"})


@dataclass(frozen=True)
class UnitResult:
    status: str
    resources: tuple[str, ...] = ()
    error: str | None = None
    session_id: str | None = None
    turn_count: int = 0
    output: str = field(default="", repr=False)
    quality: tuple[str, ...] = ()
    halt: bool = False
    output_state: str = "none"
    revision: str | None = None
    page: Page | None = field(default=None, repr=False)
    changes: tuple[str, ...] = ()
    unfinished: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in {"completed", "skipped", "failed", "stopped", "blocked"}:
            raise ValueError("Invalid unit status")
        if self.output_state not in {"none", "available", "unavailable"}:
            raise ValueError("Invalid output availability")
        if not isinstance(self.resources, tuple) or not all(
            isinstance(p, str) for p in self.resources
        ):
            raise ValueError("Invalid resource references")
        if not isinstance(self.quality, tuple) or not all(isinstance(p, str) for p in self.quality):
            raise ValueError("Invalid quality notes")
        for values in (self.changes, self.unfinished):
            if not isinstance(values, tuple) or not all(isinstance(p, str) for p in values):
                raise ValueError("Invalid result facts")
        if type(self.turn_count) is not int or self.turn_count < 0 or type(self.halt) is not bool:
            raise ValueError("Invalid unit counts or halt flag")
        if any(
            value is not None and not isinstance(value, str)
            for value in (self.error, self.session_id, self.revision)
        ):
            raise ValueError("Invalid unit details")
        if not isinstance(self.output, str):
            raise ValueError("Invalid temporary output")
        if self.page is not None and not isinstance(self.page, Page):
            raise ValueError("Invalid saved page")

    def summary(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("output")
        value.pop("page")
        if self.output_state == "available":
            value["output_state"] = "unavailable"
        return value

    @classmethod
    def from_summary(cls, value: dict[str, Any]) -> UnitResult:
        if any(not isinstance(value.get(key, []), list) for key in ("changes", "unfinished")):
            raise ValueError("Invalid result facts")
        if not isinstance(value.get("resources", []), list) or not isinstance(
            value.get("quality", []), list
        ):
            raise ValueError("Invalid result lists")
        return cls(
            status=value["status"],
            resources=tuple(value.get("resources", ())),
            error=value.get("error"),
            session_id=value.get("session_id"),
            turn_count=value.get("turn_count", 0),
            quality=tuple(value.get("quality", ())),
            halt=value.get("halt", False),
            output_state=value.get("output_state", "none"),
            revision=value.get("revision"),
            changes=tuple(value.get("changes", ())),
            unfinished=tuple(value.get("unfinished", ())),
        )


@dataclass(frozen=True)
class TaskView:
    id: str
    kb_dir: str
    operation: str
    state: str
    stage: str
    total: int
    results: tuple[UnitResult, ...]
    stop_requested: bool
    processes_reaped: bool
    started_at: str | None = None
    error: str | None = None
    text: str = field(default="", repr=False)
    text_truncated: bool = False
    retry_of: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{32}", self.id):
            raise ValueError("Invalid task identity")
        if self.retry_of is not None and not re.fullmatch(r"[0-9a-f]{32}", self.retry_of):
            raise ValueError("Invalid retry task identity")
        if not all(isinstance(v, str) for v in (self.kb_dir, self.operation, self.stage)):
            raise ValueError("Invalid task description")
        if self.state not in TERMINAL | {"queued", "waiting", "running", "stopping"}:
            raise ValueError("Invalid task state")
        if type(self.total) is not int or self.total < len(self.results) or self.total < 1:
            raise ValueError("Invalid task count")
        if any(
            type(v) is not bool
            for v in (self.stop_requested, self.processes_reaped, self.text_truncated)
        ):
            raise ValueError("Invalid task flags")
        if any(v is not None and not isinstance(v, str) for v in (self.started_at, self.error)):
            raise ValueError("Invalid task details")

    @property
    def succeeded(self) -> int:
        return sum(result.status == "completed" for result in self.results)

    @property
    def skipped(self) -> int:
        return sum(result.status == "skipped" for result in self.results)

    @property
    def failed(self) -> int:
        return sum(result.status == "failed" for result in self.results)

    @property
    def unfinished(self) -> int:
        return self.total - self.succeeded - self.skipped - self.failed

    def summary(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("text")
        value["results"] = [result.summary() for result in self.results]
        return value

    @classmethod
    def from_summary(cls, value: dict[str, Any]) -> TaskView:
        value = dict(value)
        value["results"] = tuple(UnitResult.from_summary(row) for row in value["results"])
        if value["state"] not in TERMINAL:
            value.update(state="interrupted", stage="interrupted", error="Previous run interrupted")
        return cls(**value)


@dataclass(frozen=True)
class UnitIdentity:
    task_id: str
    unit_id: str
    kb_dir: str
    request_hash: str
    version: int = PROTOCOL_VERSION
    generation: str | None = None

    def __post_init__(self) -> None:
        if self.version != PROTOCOL_VERSION or not re.fullmatch(r"[0-9a-f]{32}", self.task_id):
            raise ValueError("Invalid protocol identity")
        if not re.fullmatch(r"\d{1,5}", self.unit_id) or not re.fullmatch(
            r"[0-9a-f]{64}", self.request_hash
        ):
            raise ValueError("Invalid execution identity")
        if not isinstance(self.kb_dir, str) or not Path(self.kb_dir).is_absolute():
            raise ValueError("Invalid knowledge-base identity")
        if self.generation is not None and (
            not isinstance(self.generation, str) or not self.generation
        ):
            raise ValueError("Invalid knowledge-base generation")

    @classmethod
    def create(
        cls,
        task_id: str,
        index: int,
        kb_dir: str,
        request: UnitRequest,
        *,
        generation: str | None = None,
    ) -> UnitIdentity:
        payload = json.dumps(asdict(request), sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256((type(request).__name__ + payload).encode()).hexdigest()
        return cls(task_id, str(index), kb_dir, digest, generation=generation)


def save_receipt(directory: Path, identity: UnitIdentity, result: UnitResult) -> None:
    atomic_write_json(
        directory / identity.task_id / f"{identity.unit_id}.json",
        {"identity": asdict(identity), "result": result.summary()},
    )


def read_receipt(directory: Path, identity: UnitIdentity) -> UnitResult | None:
    try:
        value = json.loads((directory / identity.task_id / f"{identity.unit_id}.json").read_text())
        if UnitIdentity(**value["identity"]) != identity:
            return None
        return UnitResult.from_summary(value["result"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
