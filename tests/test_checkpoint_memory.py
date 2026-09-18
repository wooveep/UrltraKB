"""Long resumptions must not retain every completed request or reread old bodies."""

import gc
import tracemalloc
from types import SimpleNamespace

import pytest

from openkb.agent.evidence_checkpoints import CompilationCheckpoints
from openkb.locks import atomic_write_json
from openkb.processing import DEFAULT_PROCESSING


def checkpoints(kb):
    return CompilationCheckpoints(
        kb,
        SimpleNamespace(source_id="1" * 32, id="2" * 64),
        SimpleNamespace(id="3" * 64),
        {"model": "openai/offline", "processing": DEFAULT_PROCESSING},
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
    if index_state == "missing":
        writer.latest.unlink()
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
