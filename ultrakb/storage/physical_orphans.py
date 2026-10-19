"""Discover final CAS names left before registry commit; never follow unknown paths."""

import os
import stat

from ultrakb.locks import LockCancelled, durable_unlink

from .blobs import blob_path


def _cancel(cancelled):
    if cancelled is not None and cancelled():
        raise LockCancelled("Stopped while scanning unregistered CAS names")


def candidates(db, max_items, max_bytes, *, cancelled=None):
    """Metadata-only discovery may race GC; deletion requires fresh exclusive checks.

    The explicit scan can traverse the CAS directory, but retains only a bounded
    candidate batch. It neither opens payloads nor holds a SQL transaction.
    """
    selected, total, more = [], 0, False
    base = db.root / ".ultrakb/blobs/sha256"
    if any(path.is_symlink() for path in (base, *base.parents[:2])):
        raise ValueError("CAS orphan discovery cannot follow symbolic links")
    for number in range(256):
        _cancel(cancelled)
        prefix = f"{number:02x}"
        shard = base / prefix
        if shard.is_symlink() or not shard.is_dir():
            continue
        try:
            with os.scandir(shard) as entries:
                for entry in entries:
                    _cancel(cancelled)
                    digest = entry.name
                    if (
                        len(digest) != 64
                        or not digest.startswith(prefix)
                        or any(c not in "0123456789abcdef" for c in digest)
                        or not entry.is_file(follow_symlinks=False)
                    ):
                        continue
                    if db.connection.execute(
                        "SELECT 1 FROM ukb_blobs WHERE digest = ?", (digest,)
                    ).fetchone():
                        continue
                    try:
                        size = entry.stat(follow_symlinks=False).st_size
                    except FileNotFoundError:
                        continue  # Another collector already removed this name.
                    if len(selected) >= max_items:
                        return selected, True
                    if total + size > max_bytes:
                        more = True
                        continue
                    selected.append((digest, size))
                    total += size
        except FileNotFoundError:
            continue
    return selected, more


def recheck(db, selected, max_items, max_bytes):
    """Bounded path/registry verification under the caller's content lease."""
    current_rows, total, more = [], 0, False
    for digest, size in selected:
        # The directory scan and every prior preview are only hints. A live or
        # retiring registry row now protects the name, regardless of references.
        if db.connection.execute("SELECT 1 FROM ukb_blobs WHERE digest = ?", (digest,)).fetchone():
            continue
        path = blob_path(db.root, digest)
        try:
            current = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(current.st_mode) or current.st_size != size:
            continue
        if len(current_rows) >= max_items or total + size > max_bytes:
            more = True
            continue
        current_rows.append((digest, size))
        total += size
    return current_rows, more


def collect(db, selected, max_items, max_bytes):
    """Caller owns exclusive reclamation and has checked pending receipt ownership."""
    selected, _ = recheck(db, selected, max_items, max_bytes)
    count = total = 0
    for digest, size in selected:
        path = blob_path(db.root, digest)
        durable_unlink(path)
        count += 1
        total += size
    return count, total
