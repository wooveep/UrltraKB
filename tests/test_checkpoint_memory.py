"""Long resumptions must not retain every completed request or reread old bodies."""

import gc
import json
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

from openkb.agent.compilation_index import (
    artifact_page,
    artifact_summaries,
    index_path,
    pending_index_path,
    recover_pending_entries,
    verified_candidates,
)
from openkb.agent.evidence_checkpoints import CompilationCheckpoints
from openkb.application.source_artifacts import saved_compilation_artifact_index
from openkb.locks import atomic_write_json
from openkb.processing import DEFAULT_PROCESSING


def checkpoints(kb):
    return CompilationCheckpoints(
        kb,
        SimpleNamespace(source_id="1" * 32, id="2" * 64),
        SimpleNamespace(id="3" * 64),
        {
            "model": "openai/offline",
            "processing": {
                **DEFAULT_PROCESSING,
                "context_tokens": 128_000,
                "max_context_tokens": 128_000,
                "output_tokens": 4_096,
                "max_output_tokens": 4_096,
            },
        },
        None,
    )


def test_completed_cache_adoption_releases_all_temporary_inputs(kb_dir, tmp_path, monkeypatch):
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    with checkpoints(kb_dir) as writer:
        for number in range(16):
            payload = {"stage": "generation", "text": f"{number}:" + "x" * (1024 * 1024)}
            with writer.request("fixture", payload) as key:
                writer.save(key, {"number": number})
        for number in range(16):
            payload = {"stage": "generation", "text": f"{number}:" + "x" * (1024 * 1024)}
            with writer.request("fixture", payload) as key:
                assert writer.load(key) == {"number": number}
            assert not list(tmp_path.glob("openkb-checkpoint-contracts/.inputs/*/data/*.json"))
    assert not list(tmp_path.glob("openkb-checkpoint-contracts/.inputs/*/owner.json"))
    assert checkpoints(kb_dir).load(key) == {"number": 15}


def test_another_consumer_can_save_after_same_key_owner_exits(kb_dir):
    with checkpoints(kb_dir) as writer:
        payload = {"stage": "generation", "text": "shared source"}
        with writer.request("fixture", payload) as active:
            with writer.request("fixture", payload) as adopting:
                assert adopting == active
            writer.save(active, {"pages": ["verified"]})
            assert writer.record(active)["contract"]["payload"] == payload


def test_insufficient_temporary_disk_stops_before_allocating_a_contract(kb_dir, monkeypatch):
    import shutil

    from openkb.processing import ProcessingIncomplete

    monkeypatch.setattr(
        shutil, "disk_usage", lambda path: SimpleNamespace(total=2**30, used=2**30, free=0)
    )
    with checkpoints(kb_dir) as writer:
        with pytest.raises(ProcessingIncomplete, match="resource_disk_insufficient"):
            with writer.request("fixture", {"stage": "generation", "text": "source"}):
                pytest.fail("A request was admitted with no disk space")


def test_contract_capacity_preserves_active_input_and_releases_capacity():
    from openkb.agent.checkpoint_contracts import PendingContracts
    from openkb.processing import ProcessingIncomplete
    from openkb.sources import content_id

    first = {"payload": {"stage": "generation", "text": "x" * 600}}
    second = {"payload": {"stage": "generation", "text": "y" * 600}}
    store = PendingContracts(max_bytes=1000)
    try:
        store.save(content_id(first), first)
        with pytest.raises(ProcessingIncomplete, match="resource_input_exceeds_budget"):
            store.save(content_id(second), second)
        assert store.read(content_id(first)) == first
        assert store.read(content_id(second)) is None
        store.discard(content_id(first))
        store.save(content_id(second), second)
        assert store.read(content_id(second)) == second
    finally:
        store.close()


def test_restart_reclaims_crashed_contracts_without_removing_live_inputs(
    kb_dir, tmp_path, monkeypatch
):
    import subprocess
    import sys
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    program = """
import os, sys, tempfile
from pathlib import Path
from tests.test_checkpoint_memory import checkpoints
tempfile.tempdir = sys.argv[1]
writer = checkpoints(Path(sys.argv[2]))
writer.key('crashed', {'stage': 'generation', 'text': 'private crash input'})
os._exit(86)
"""
    crashed = subprocess.run([sys.executable, "-c", program, str(tmp_path), str(kb_dir)])
    assert crashed.returncode == 86
    abandoned = set(tmp_path.glob("openkb-checkpoint-contracts*/**/*.json"))
    assert abandoned
    with checkpoints(kb_dir) as live:
        with live.request("live", {"stage": "generation", "text": "still needed"}) as key:
            with checkpoints(kb_dir) as restarted:
                with restarted.request("next", {"stage": "generation", "text": "next"}):
                    assert not any(path.exists() for path in abandoned)
            live.save(key, {"ok": True})
            assert live.record(key)["contract"]["payload"]["text"] == "still needed"


def test_private_rows_preserve_order_and_values_without_retaining_all_bodies(kb_dir):
    with checkpoints(kb_dir) as writer:
        rows = writer.private_rows("source_units")
        tracemalloc.start()
        try:
            for number in range(16):
                rows[str(number)] = {"text": f"{number}:" + "x" * 1024**2}
            gc.collect()
            retained, _ = tracemalloc.get_traced_memory()
            assert retained < 4 * 1024**2
        finally:
            tracemalloc.stop()
        assert list(rows) == [str(number) for number in range(16)]
        assert rows["15"]["text"].startswith("15:")
        assert len(rows) == 16
        del rows["0"]
        assert "0" not in rows and len(rows) == 15


def test_resuming_many_large_requests_has_bounded_retained_memory(kb_dir):
    writer = checkpoints(kb_dir)
    keys = []
    for number in range(16):
        key = writer.key(
            "fixture", {"stage": "generation", "text": str(number) + ":" + "x" * (1024 * 1024)}
        )
        writer.save(key, {"number": number})
        keys.append(key)
    del writer
    gc.collect()
    reader = checkpoints(kb_dir)
    tracemalloc.start()
    try:
        for number, key in enumerate(keys):
            assert reader.load(key) == {"number": number}
        gc.collect()
        retained, _ = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert retained < 4 * 1024 * 1024, f"Completed checkpoint bodies retained {retained} bytes"
    # Releasing cached bodies must not lose their durable recovery content.
    assert reader.load(keys[0]) == {"number": 0}


def test_appending_checkpoint_uses_stage_index_without_reading_old_payloads(kb_dir, monkeypatch):
    writer = checkpoints(kb_dir)
    previous = writer.key("fixture", {"stage": "facts", "text": "previous source"})
    writer.save(previous, {"value": 1})
    reader = checkpoints(kb_dir)
    import openkb.agent.evidence_checkpoints as module

    read = module.read_object

    def bounded_read(path):
        assert path.stem != previous, "Appending a request reloaded a previous request body"
        return read(path)

    monkeypatch.setattr(module, "read_object", bounded_read)
    current = reader.key("fixture", {"stage": "generation", "text": "next source"})
    reader.save(current, {"value": 2})
    assert reader.checkpoint_keys("facts") == [previous]
    assert reader.checkpoint_keys("generation") == [current]


def test_appending_checkpoint_does_not_read_or_rewrite_a_historical_json_index(kb_dir, monkeypatch):
    """A new result writes one SQLite row instead of copying every saved summary."""

    writer = checkpoints(kb_dir)
    for number in range(128):
        key = writer.key("fixture", {"stage": "facts", "text": f"saved {number}"})
        writer.save(key, {"value": number})
    assert not writer.latest.exists()

    import openkb.agent.evidence_checkpoints as module

    read = module.read_object

    def reject_historical_index(path):
        assert path != writer.latest, "Appending a checkpoint reread the aggregate history index"
        return read(path)

    monkeypatch.setattr(module, "read_object", reject_historical_index)
    current = writer.key("fixture", {"stage": "generation", "text": "next source"})
    writer.save(current, {"value": "next"})

    assert writer.checkpoint_keys("generation") == [current]


def test_interrupted_index_append_recovers_the_written_checkpoint(kb_dir, monkeypatch):
    """A receipt that outlives its SQLite append remains resumable and retained."""

    writer = checkpoints(kb_dir)
    previous = writer.key("fixture", {"stage": "facts", "text": "previous source"})
    writer.save(previous, {"value": 1})
    current = writer.key("fixture", {"stage": "generation", "text": "next source"})

    import openkb.agent.evidence_checkpoints as module

    monkeypatch.setattr(
        module,
        "update_index",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("index interruption")),
    )
    with pytest.raises(OSError, match="index interruption"):
        writer.save(current, {"value": 2})
    assert writer.record(current) is not None
    marker = pending_index_path(writer.store, writer.input["version"], current, "checkpoint")
    assert marker.exists()

    reader = checkpoints(kb_dir)
    assert current in reader.checkpoint_keys("generation")
    assert reader.record(current) is not None
    assert not marker.exists()


def test_pending_marker_for_another_parse_does_not_block_its_own_parse(kb_dir, monkeypatch):
    """An interrupted parse A must not make parse B's artifact read fail."""

    writer = checkpoints(kb_dir)
    previous = writer.key("fixture", {"stage": "facts", "text": "previous source"})
    writer.save(previous, {"value": 1})
    current = writer.key("fixture", {"stage": "generation", "text": "next source"})

    import openkb.agent.evidence_checkpoints as module

    monkeypatch.setattr(
        module,
        "update_index",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("index interruption")),
    )
    with pytest.raises(OSError, match="index interruption"):
        writer.save(current, {"value": 2})
    marker = pending_index_path(writer.store, writer.input["version"], current, "checkpoint")

    assert not recover_pending_entries(
        writer.store,
        writer.input["version"],
        source_id=writer.input["source"],
        parse_id="4" * 64,
    )
    assert marker.exists()
    assert recover_pending_entries(
        writer.store,
        writer.input["version"],
        source_id=writer.input["source"],
        parse_id=writer.input["parse"],
    )
    assert not marker.exists()


def test_concurrent_readers_recover_one_pending_index_marker(kb_dir, monkeypatch):
    """Concurrent read-only resumes claim one marker without SQLite contention."""

    writer = checkpoints(kb_dir)
    previous = writer.key("fixture", {"stage": "facts", "text": "previous source"})
    writer.save(previous, {"value": 1})
    current = writer.key("fixture", {"stage": "generation", "text": "next source"})

    import openkb.agent.evidence_checkpoints as module

    monkeypatch.setattr(
        module,
        "update_index",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("index interruption")),
    )
    with pytest.raises(OSError, match="index interruption"):
        writer.save(current, {"value": 2})
    marker = pending_index_path(writer.store, writer.input["version"], current, "checkpoint")
    gate = Barrier(2)

    def resume():
        gate.wait()
        return checkpoints(kb_dir).checkpoint_keys("generation")

    with ThreadPoolExecutor(max_workers=2) as workers:
        resumed = list(workers.map(lambda _: resume(), range(2)))

    assert all(current in keys for keys in resumed)
    assert not marker.exists()


def test_missing_sqlite_index_rebuilds_recovery_artifact_summaries(kb_dir):
    """One exceptional rebuild retains draft and plan previews before the next save."""

    writer = checkpoints(kb_dir)
    draft = writer.key("fixture", {"stage": "generation", "text": "draft source"})
    writer.save_recovery(
        draft,
        "draft",
        {"output": {"page_key": "page", "content": "draft candidate"}},
    )
    plan = writer.key("fixture", {"stage": "planning", "text": "plan source"})
    writer.save_recovery(
        plan,
        "plan",
        {"metadata": {"protocol": "document-plan-v1"}, "page_changes": []},
    )
    index_path(writer.store, writer.input["version"]).unlink()

    reader = checkpoints(kb_dir)
    assert reader.checkpoint_keys("generation") == []
    rows = saved_compilation_artifact_index(
        reader.store,
        reader.input["source"],
        reader.input["version"],
        reader.input["parse"],
    )
    assert {(row["storage"], row["key"]) for row in rows} >= {("draft", draft), ("plan", plan)}

    current = reader.key("fixture", {"stage": "facts", "text": "next source"})
    reader.save(current, {"value": "next"})
    migrated = saved_compilation_artifact_index(
        reader.store,
        reader.input["source"],
        reader.input["version"],
        reader.input["parse"],
    )
    assert {(row["storage"], row["key"]) for row in migrated} >= {("draft", draft), ("plan", plan)}


def test_corrupt_sqlite_index_rebuilds_checkpoint_and_artifact_projections(kb_dir):
    """A corrupted derived database cannot hide valid immutable receipts."""

    writer = checkpoints(kb_dir)
    current = writer.key("fixture", {"stage": "facts", "text": "saved source"})
    writer.save(current, {"value": "saved"})
    path = index_path(writer.store, writer.input["version"])
    path.write_bytes(b"not sqlite")

    reader = checkpoints(kb_dir)
    assert reader.checkpoint_keys("facts") == [current]
    assert reader.load(current) == {"value": "saved"}
    quarantine = writer.root / "quarantine"
    assert list(quarantine.glob(f"{writer.input['version']}-index-*.sqlite3"))

    path.write_bytes(b"not sqlite")
    page = artifact_page(
        writer.store,
        writer.input["source"],
        writer.input["version"],
        writer.input["parse"],
        "facts",
        offset=0,
        limit=10,
    )
    assert page is not None and page[1] == 1
    assert [row["key"] for row in page[0]] == [current]


def test_semantically_corrupt_sqlite_rows_rebuild_from_immutable_receipts(kb_dir):
    """Valid SQLite bytes cannot make poisoned derived rows authoritative."""

    import sqlite3

    writer = checkpoints(kb_dir)
    current = writer.key("fixture", {"stage": "facts", "text": "saved source"})
    writer.save(current, {"value": "saved"})
    path = index_path(writer.store, writer.input["version"])

    with sqlite3.connect(path) as db, db:
        db.execute("UPDATE checkpoints SET key = 'not-a-digest'")
    assert checkpoints(kb_dir).checkpoint_keys("facts") == [current]

    with sqlite3.connect(path) as db, db:
        db.execute("UPDATE checkpoints SET stage = 'bogus'")
    assert checkpoints(kb_dir).checkpoint_keys("facts") == [current]

    with sqlite3.connect(path) as db, db:
        db.execute("UPDATE artifacts SET summary = '{}'")
    page = artifact_page(
        writer.store,
        writer.input["source"],
        writer.input["version"],
        writer.input["parse"],
        "facts",
        offset=0,
        limit=10,
    )
    assert page is not None and [row["key"] for row in page[0]] == [current]

    with sqlite3.connect(path) as db, db:
        db.execute("UPDATE artifacts SET stage = 'generation'")
    page = artifact_page(
        writer.store,
        writer.input["source"],
        writer.input["version"],
        writer.input["parse"],
        "generation",
        offset=0,
        limit=10,
    )
    assert page == ((), 0)
    page = artifact_page(
        writer.store,
        writer.input["source"],
        writer.input["version"],
        writer.input["parse"],
        "facts",
        offset=0,
        limit=10,
    )
    assert page is not None and [row["key"] for row in page[0]] == [current]

    poisoned = {
        "schema": 1,
        "storage": "checkpoint",
        "key": current,
        "stage": "verification",
        "source": writer.input["source"],
        "version": writer.input["version"],
        "parse": writer.input["parse"],
        "model": "openai/offline",
        "draft": False,
        "adopted": False,
        "verified": {},
    }
    with sqlite3.connect(path) as db, db:
        db.execute(
            "UPDATE artifacts SET stage = 'verification', verified_page_key = 'page', "
            "verified_candidate = 'candidate', summary = ?",
            (json.dumps(poisoned),),
        )
    assert (
        verified_candidates(
            writer.store,
            writer.input["source"],
            writer.input["version"],
            writer.input["parse"],
            [{"page_key": "page", "candidate": "candidate"}],
        )
        == {}
    )


def test_malformed_pending_marker_does_not_block_valid_index_reads(kb_dir):
    """An unauthenticated marker is discarded without invalidating completed work."""

    writer = checkpoints(kb_dir)
    current = writer.key("fixture", {"stage": "facts", "text": "saved source"})
    writer.save(current, {"value": "saved"})
    marker = (
        index_path(writer.store, writer.input["version"]).parent
        / f"{writer.input['version']}-pending"
        / "malformed.json"
    )
    atomic_write_json(marker, {})

    assert checkpoints(kb_dir).checkpoint_keys("facts") == [current]
    assert not marker.exists()


def test_artifact_stream_repairs_a_later_bad_row_without_repeating_prior_rows(kb_dir):
    """A streaming projection resumes after its verified cursor on repair."""

    import sqlite3

    writer = checkpoints(kb_dir)
    keys = []
    for text in ("first source", "second source"):
        key = writer.key("fixture", {"stage": "facts", "text": text})
        writer.save(key, {"value": text})
        keys.append(key)
    path = index_path(writer.store, writer.input["version"])
    with sqlite3.connect(path) as db:
        later = db.execute(
            "SELECT artifact_key FROM artifacts WHERE stage = 'facts' "
            "ORDER BY artifact_key DESC LIMIT 1"
        ).fetchone()[0]
        db.execute("UPDATE artifacts SET summary = '{}' WHERE artifact_key = ?", (later,))
        db.commit()

    streamed = tuple(
        artifact_summaries(
            writer.store,
            writer.input["source"],
            writer.input["version"],
            writer.input["parse"],
            stages=("facts",),
        )
        or ()
    )
    assert [row["key"] for row in streamed] == sorted(keys)


def test_pending_contracts_do_not_accumulate_prompt_bodies(kb_dir):
    writer = checkpoints(kb_dir)
    keys = []
    tracemalloc.start()
    try:
        for number in range(16):
            keys.append(
                writer.key(
                    "fixture",
                    {"stage": "generation", "text": str(number) + ":" + "x" * (1024 * 1024)},
                )
            )
        gc.collect()
        retained, _ = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert retained < 4 * 1024 * 1024, f"Pending prompt bodies retained {retained} bytes"
    # An old pending key must still publish its complete, identity-bound contract.
    writer.save(keys[0], {"number": 0})
    record = writer.record(keys[0])
    assert record["contract"]["payload"]["text"].startswith("0:")
    assert record["key"] == keys[0]


@pytest.mark.parametrize("index_state", ["missing", "legacy", "invalid"])
def test_index_recovery_preserves_previous_results(kb_dir, index_state):
    writer = checkpoints(kb_dir)
    previous = writer.key("fixture", {"stage": "facts", "text": "saved source"})
    writer.save(previous, {"facts": ["original"]})
    index_path(writer.store, writer.input["version"]).unlink()
    if index_state == "missing":
        pass
    else:
        index = {"checkpoints": [previous]}
        if index_state == "invalid":
            index["stages"] = {"facts": ["0" * 64]}
        atomic_write_json(writer.latest, index)
    reader = checkpoints(kb_dir)
    assert previous in reader.checkpoint_keys("facts")
    current = reader.key("fixture", {"stage": "generation", "text": "next source"})
    reader.save(current, {"pages": ["generated"]})
    assert previous in reader.checkpoint_keys("facts")
    assert current in reader.checkpoint_keys("generation")
    # Consumers own returned values; edits cannot contaminate later resumptions.
    reader.load(previous)["facts"].append("changed")
    assert reader.load(previous) == {"facts": ["original"]}


@pytest.mark.parametrize("contract", [{"payload": {"stage": "generation"}}, {"payload": []}])
def test_corrupt_temporary_contract_cannot_become_a_durable_checkpoint(
    kb_dir, monkeypatch, contract
):
    writer = checkpoints(kb_dir)
    key = writer.key("fixture", {"stage": "generation", "text": "saved source"})
    import openkb.agent.checkpoint_contracts as module

    monkeypatch.setattr(module, "read_object", lambda path: contract)
    with pytest.raises(ValueError, match="contract identity"):
        writer.save(key, {"pages": ["generated"]})
    assert not (writer.root / f"{key}.json").exists()
