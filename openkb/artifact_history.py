"""Keep generator history immutable to model tools during a producing run."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class HistoryProtection:
    roots: tuple[Path, ...]
    other_files: frozenset[Path]
    issues: list[str] = field(default_factory=list)


_active: ContextVar[HistoryProtection | None] = ContextVar("artifact_history", default=None)


@contextmanager
def preserve_artifact_history(kb_dir: Path, *, target: Path):
    roots = tuple(
        path.resolve()
        for path in (kb_dir / "output").rglob("*-workspace")
        if path.is_dir() and any(path.glob("iteration-*"))
    )
    other_files = frozenset(
        path.resolve()
        for folder in (kb_dir / "output", kb_dir / "wiki/explorations")
        for path in folder.rglob("*")
        if path.is_file() and not path.resolve().is_relative_to(target.resolve())
    )
    protection = HistoryProtection(roots, other_files)
    token = _active.set(protection)
    try:
        yield protection
    finally:
        _active.reset(token)


def history_write_allowed(path: Path) -> bool:
    protection = _active.get()
    if protection and any(path.resolve().is_relative_to(root) for root in protection.roots):
        if "history_write_denied" not in protection.issues:
            protection.issues.append("history_write_denied")
        return False
    if protection and path.resolve() in protection.other_files:
        if "unconfirmed_output_write_denied" not in protection.issues:
            protection.issues.append("unconfirmed_output_write_denied")
        return False
    return True
