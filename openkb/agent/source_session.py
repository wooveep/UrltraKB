"""One task's detached evidence capabilities and observed original bindings."""

from __future__ import annotations

import copy
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any


@dataclass
class SourceSession:
    kb_dir: Path
    capabilities: dict[str, Any] = field(default_factory=dict)
    observations: dict[str, list[dict]] = field(default_factory=dict)
    active: bool = True
    lock: Any = field(default_factory=RLock, repr=False)

    def observe(self, rows: list[dict]) -> None:
        from openkb.agent.answer_references import short_citation

        with self.lock:
            if not self.active:
                raise ValueError("Source task has ended")
            for row in rows:
                marker = short_citation(row["citation"])
                values = self.observations.setdefault(marker, [])
                if row not in values:
                    values.append(copy.deepcopy(row))


_ACTIVE: ContextVar[SourceSession | None] = ContextVar("source_task", default=None)


def current_source_session(kb_dir: Path) -> SourceSession | None:
    session = _ACTIVE.get()
    if session and (not session.active or session.kb_dir != kb_dir.resolve()):
        raise ValueError("Source task belongs to another knowledge base or has ended")
    return session


@contextmanager
def source_session(kb_dir: Path):
    current = current_source_session(kb_dir)
    if current:
        yield current
        return
    session = SourceSession(kb_dir.resolve())
    token = _ACTIVE.set(session)
    try:
        yield session
    finally:
        session.active = False
        _ACTIVE.reset(token)


def task_tools(kb_dir: Path, name: str, build):
    """Build on the lease owner, then reuse in nested SDK tasks without relocking."""
    session = current_source_session(kb_dir)
    if session is None:
        return build(kb_dir)
    if name not in session.capabilities:
        session.capabilities[name] = build(kb_dir)
    return session.capabilities[name]
