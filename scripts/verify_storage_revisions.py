"""Measure real revision deduplication and disposable compiler baseline preparation."""

import argparse
import hashlib
import json
import os
import platform
import sqlite3
import struct
import tempfile
import zlib
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.storage_execution import begin_storage_operation
from ultrakb.locks import atomic_write_text
from ultrakb.storage.blobs import blob_path
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.coordination import storage_stage, try_reclamation
from ultrakb.storage.database import create_database, database_path
from ultrakb.storage.knowledge_store import KnowledgeStore


def sample_png(width=1024, height=1024):
    def chunk(kind, payload):
        content = kind + payload
        return struct.pack(">I", len(payload)) + content + struct.pack(">I", zlib.crc32(content))

    pixels = hashlib.shake_256(b"UltraKB public calibration image").digest(width * height * 3)
    rows = b"".join(
        b"\0" + pixels[start : start + width * 3] for start in range(0, len(pixels), width * 3)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def snapshot(root, identity, stable):
    with storage_stage(root, identity, read_only=True) as db:
        rows = db.connection.execute("SELECT digest, byte_count FROM ukb_blobs").fetchall()
        references = db.connection.execute("SELECT count(*) FROM ukb_blob_refs").fetchone()[0]
        revisions = db.connection.execute(
            "SELECT count(*) FROM ukb_knowledge_revisions"
        ).fetchone()[0]
        counts = {
            digest: db.connection.execute(
                "SELECT count(*) FROM ukb_blobs WHERE digest = ?", (digest,)
            ).fetchone()[0]
            for digest in stable
        }
    paths = [blob_path(root, digest) for digest, _ in rows]
    wal = database_path(root).with_suffix(".sqlite-wal")
    physical = {path for path in (root / ".ultrakb/blobs/sha256").rglob("*") if path.is_file()}
    assert physical == set(paths)
    sqlite_files = sorted(path.relative_to(root).as_posix() for path in root.rglob("*.sqlite"))
    assert sqlite_files == [".ultrakb/kb.sqlite"]
    assert not any(
        (root / ".ultrakb" / name).exists()
        for name in ("knowledge", "snapshots", "staging", "proposals")
    )
    return {
        "registered_blobs": len(rows),
        "registered_payload_bytes": sum(size for _, size in rows),
        "blob_reference_rows": references,
        "knowledge_revisions": revisions,
        "stable_digest_rows": counts,
        "physical_cas_file_bytes": sum(path.stat().st_size for path in paths),
        "physical_cas_allocated_bytes": sum(path.stat().st_blocks * 512 for path in paths)
        if hasattr(paths[0].stat(), "st_blocks")
        else None,
        "main_database_bytes": database_path(root).stat().st_size,
        "wal_bytes": wal.stat().st_size if wal.exists() else 0,
        "sqlite_files": sqlite_files,
        "unregistered_cas_files": len(physical - set(paths)),
        "legacy_snapshot_directories": 0,
    }


def prepare_sample(bound, files, required):
    copied, syncs = {}, []
    actual = os.fsync

    def observed(fd):
        syncs.append(fd)
        return actual(fd)

    began = perf_counter()
    with (
        patch("os.fsync", observed),
        bound.compile_workspace(required_attachments=required) as baseline,
    ):
        elapsed = perf_counter() - began
        directory = baseline.directory
        assert baseline.files == bound.files()
        for path in directory.rglob("*"):
            if path.is_file():
                name = path.relative_to(directory).as_posix()
                data = path.read_bytes()
                assert data == files[name]
                copied[name] = len(data)
        expected = {path for path in files if path.endswith(".md")} | set(required)
        assert set(copied) == expected
        assert baseline.bytes_copied == sum(copied.values())
        assert not syncs
        with try_reclamation(bound.root, bound.identity) as db:
            assert db is not None
        (directory / "index.md").write_bytes(b"private candidate")
        assert bound.read("index.md") == files["index.md"]
    assert not directory.exists()
    return {
        "preparation_seconds": elapsed,
        "markdown_files": sum(path.endswith(".md") for path in copied),
        "markdown_bytes": sum(size for path, size in copied.items() if path.endswith(".md")),
        "required_attachment_bytes": sum(copied[path] for path in required),
        "unrelated_attachment_bytes_copied": 0,
        "index_database_bytes_copied": 0,
        "baseline_fsync_calls": len(syncs),
        "total_bytes_copied": sum(copied.values()),
        "manifest_logical_bytes": sum(map(len, files.values())),
        "all_markdown_and_required_bytes_equal": True,
        "private_mutation_preserves_cas": True,
        "reclamation_available_after_materialization": True,
    }


def run(directory, revisions, pages):
    root = directory / "knowledge"
    identity = create_database(root)
    context = ExecutionContext(storage_identity=identity)
    with begin_storage_operation(root, context, "revision_calibration"):
        result = run_revisions(directory, root, identity, revisions, pages)
        context._storage_result = "completed"
    with storage_stage(root, identity, read_only=True) as db:
        assert db.connection.execute(
            "SELECT state FROM ukb_executions WHERE execution_id = ?",
            (context.storage_execution.execution_id,),
        ).fetchone() == ("completed",)
    result["operation_state"] = "completed"
    return result


def run_revisions(directory, root, identity, revisions, pages):
    knowledge = KnowledgeStore(root, identity)
    source = directory / "calibration-original.md"
    original = b"# Retained original\n" + b"Public calibration facts.\n" * 160000
    source.write_bytes(original)
    catalog = CatalogStore(root, identity)
    admitted = catalog.admit(source, source_key="calibration")
    image = sample_png()
    unrelated = hashlib.shake_256(b"unrelated public attachment").digest(16 * 1024**2)
    files = {
        "index.md": b"# Knowledge\n"
        + b"\n".join(f"[[concepts/page-{i}]]".encode() for i in range(pages)),
        "AGENTS.md": b"Public compiler preparation calibration policy.",
        "images/required.png": image,
        "attachments/unrelated.bin": unrelated,
        "index/context.sqlite": b"opaque excluded index fixture" * 20000,
    }
    for number in range(pages):
        header = f"# Calibration page {number}\nAliases: public-{number}\n".encode()
        files[f"concepts/page-{number}.md"] = header + b"Stable public knowledge facts.\n" * 2100
    stable = [hashlib.sha256(payload).hexdigest() for payload in (original, image, unrelated)]
    first = knowledge.publish(knowledge.prepare_revision(files, expected=None))
    before = snapshot(root, identity, stable)
    first_bound = knowledge.bind(first.publication_id)
    baseline = prepare_sample(first_bound, files, ("images/required.png",))
    began = perf_counter()
    for revision in range(1, revisions + 1):
        base = knowledge.bind()
        page = f"# Changed body {revision}\n![retained image](images/required.png)\n".encode()
        knowledge.publish(knowledge.prepare_changes(base, {"index.md": page}))
        files["index.md"] = page
    elapsed = perf_counter() - began
    after = snapshot(root, identity, stable)
    assert before["stable_digest_rows"] == after["stable_digest_rows"] == dict.fromkeys(stable, 1)
    assert after["knowledge_revisions"] == revisions + 1
    source.unlink()
    assert catalog.read_original(admitted.revision_id) == original
    assert first_bound.read("index.md").startswith(b"# Knowledge\n")
    assert knowledge.bind().read("index.md") == files["index.md"]
    for digest, payload in zip(stable, (original, image, unrelated), strict=True):
        assert blob_path(root, digest).read_bytes() == payload
    return {
        "schema_version": 1,
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(),
        },
        "workload": {
            "additional_revisions": revisions,
            "stable_markdown_pages": pages,
            "stable_original_bytes": len(original),
            "stable_image_bytes": len(image),
            "unrelated_attachment_bytes": len(unrelated),
            "actual_model_sends": 0,
            "revision_seconds": elapsed,
        },
        "before": before,
        "after": after,
        "compiler_baseline": baseline,
        "old_publication_and_deleted_external_original_readable": True,
        "interpretation": (
            "One host; storage preparation only, not model throughput "
            "or a legacy speedup comparison."
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revisions", type=int, default=100)
    parser.add_argument("--pages", type=int, default=64)
    args = parser.parse_args()
    if not 1 <= args.revisions <= 1000 or not 1 <= args.pages <= 128:
        parser.error("Revisions must be 1..1000 and pages 1..128")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="revisions-", dir=args.output.parent) as temporary:
        result = run(Path(temporary), args.revisions, args.pages)
    encoded = json.dumps(result, indent=2) + "\n"
    atomic_write_text(args.output, encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()
