"""Publish complete private inputs and keep raw ownership under one KB lease."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from openkb.inputs import copy_stable
from openkb.locks import LockCancelled, kb_ingest_lock
from openkb.mutation import mutation_scope
from openkb.state import HashRegistry


@dataclass(frozen=True)
class PublishedInput:
    kb_dir: Path
    path: Path
    digest: str

    def discard_if_unregistered(self) -> None:
        """A dedup response never authorizes removing a retained registry source."""
        entries = HashRegistry(self.kb_dir / ".openkb/hashes.json").all_entries()
        for entry in entries.values():
            for key in ("path", "raw_path", "source_path"):
                raw = entry.get(key)
                if raw and (self.kb_dir / raw).resolve() == self.path.resolve():
                    return
        if not self.path.is_file() or HashRegistry.hash_file(self.path) != self.digest:
            return
        with mutation_scope(self.kb_dir, [self.path], operation="discard-duplicate-upload"):
            self.path.unlink()


@contextmanager
def published_input(
    kb_dir: Path,
    source: Path,
    *,
    cancelled: Callable[[], bool] | None = None,
    filename: str | None = None,
) -> Iterator[PublishedInput]:
    """Reserve, publish and consume one ready upload without a watch race.

    Keep the published raw on business failure for an explicit later retry.
    The caller owns its private source's lifetime separately.
    """
    name = filename if filename is not None else source.name
    if not name or Path(name).name != name or "\\" in name or name in {".", ".."}:
        raise ValueError("Input filename must be a basename")
    with kb_ingest_lock(kb_dir / ".openkb", cancelled=cancelled):
        raw = kb_dir / "raw"
        if not raw.resolve().is_relative_to(kb_dir.resolve()):
            raise ValueError("Raw directory is outside the knowledge base")
        raw.mkdir(exist_ok=True)
        target = raw / name
        number = 1
        while target.exists() or target.is_symlink():
            target = raw / f"{Path(name).stem}-{number}{Path(name).suffix}"
            number += 1
        if not target.resolve().is_relative_to(raw.resolve()):
            raise ValueError("Upload target is outside the raw directory")
        if cancelled and cancelled():
            raise LockCancelled("Upload cancelled before publication")
        with mutation_scope(kb_dir, [target], operation="publish-upload"):
            digest = copy_stable(source, target)
        yield PublishedInput(kb_dir, target, digest)
