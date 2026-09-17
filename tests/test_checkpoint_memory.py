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
