"""Retain pending packets exactly; accept known responses before changing generation."""

import os
import sqlite3
from pathlib import Path

from ultrakb.locks import durable_unlink

from .backup_state import BackupPendingReceipt, fingerprint, pending_payload, state_path
from .blobs import BlobRetiring
from .database import open_database
from .receipt_store import ReceiptStore, read_pending_packet
from .usage_store import require_request_generation


def capture_pending(root, snapshot, identity, copy_verified):
    """Caller holds the existing KB write coordination used by packet writers/repair."""
    directory = state_path(root, ".ultrakb/receipts/pending")
    if not directory.exists():
        return ()
    records = []
    with open_database(snapshot, expected=identity, read_only=True, sealed_archive=True) as db:
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                if entry.is_symlink() or not entry.is_file():
                    raise ValueError(
                        "Backup cannot safely capture an unknown pending receipt entry"
                    )
                state = "repairable"
                try:
                    packet, _ = read_pending_packet(path, identity, byte_limit=64 * 1024**2)
                    require_request_generation(db, packet.request_id)
                except (ValueError, KeyError, OSError):
                    state = "unresolved"
                digest, size = fingerprint(path)
                record = BackupPendingReceipt(
                    name=entry.name, digest=digest, byte_count=size, state=state
                )
                copy_verified(path, pending_payload(snapshot, record.name), digest)
                records.append(record)
    return tuple(sorted(records, key=lambda record: record.name))


def restore_pending(backup, destination, identity, manifest, copy_verified):
    unresolved = 0
    store = ReceiptStore(destination, identity)
    for record in manifest.pending_receipts:
        target = pending_payload(destination, record.name)
        copy_verified(pending_payload(backup, record.name), target, record.digest)
        if record.state == "unresolved":
            unresolved += 1
            continue
        try:
            packet, response = read_pending_packet(target, identity, byte_limit=64 * 1024**2)
            store._accept(packet.request_id, response, packet.version, packet.usage)
        except (ValueError, KeyError, OSError, sqlite3.Error, BlobRetiring):
            unresolved += 1
            continue
        durable_unlink(target)
    return "partial" if unresolved else "complete"
