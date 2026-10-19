"""Publish complete private inputs and keep raw ownership under one KB lease."""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from ultrakb.file_digest import hash_file
from ultrakb.inputs import copy_stable
from ultrakb.locks import LockCancelled, durable_unlink, kb_ingest_lock, sync_file_and_parents


@dataclass(frozen=True)
class PublishedInput:
    kb_dir: Path
    path: Path
    digest: str

    def discard_if_unregistered(self) -> None:
        """A dedup response never authorizes removing a retained registry source."""
        from ultrakb.storage.catalog_store import CatalogStore
        from ultrakb.storage.database import open_database

        with kb_ingest_lock(self.kb_dir / ".ultrakb"):
            with open_database(self.kb_dir, read_only=True) as db:
                catalog = CatalogStore(self.kb_dir, db.identity)
            if catalog.has_source_key(str(self.path.resolve())):
                return
            if not self.path.is_file() or hash_file(self.path) != self.digest:
                return
            durable_unlink(self.path)


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
    with kb_ingest_lock(kb_dir / ".ultrakb", cancelled=cancelled):
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
        descriptor, temporary = tempfile.mkstemp(prefix=".ultrakb-upload-", suffix=".tmp", dir=raw)
        os.close(descriptor)
        private = Path(temporary)
        try:
            digest = copy_stable(source, private)
            if cancelled and cancelled():
                raise LockCancelled("Upload cancelled before publication")
            # A new link exposes only verified bytes and cannot replace an
            # external file created after the initial name reservation.
            while True:
                try:
                    os.link(private, target)
                    break
                except FileExistsError:
                    target = raw / f"{Path(name).stem}-{number}{Path(name).suffix}"
                    number += 1
            sync_file_and_parents(target, ancestor=kb_dir)
        finally:
            private.unlink(missing_ok=True)
    yield PublishedInput(kb_dir, target, digest)
