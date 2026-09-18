"""Restarted task allowances fail closed and recompilation cannot reset them."""

import json

import pytest

from openkb.locks import atomic_write_json
from openkb.processing import DEFAULT_PROCESSING, RequestLimits
from openkb.runtime.family_budget import FamilyBudget


@pytest.mark.parametrize(
    "change",
    [
        {"requests": -1},
        {"tokens": -100},
        {"seconds": float("nan")},
        {"max_tokens": -1},
        {"reservations": []},
        {"reservations": {"a" * 32: {"tokens": 5, "seconds": 0}}},
    ],
)
def test_corrupt_allowance_cannot_authorize_a_request(tmp_path, change):
    path = tmp_path / ("a" * 32 + ".json")
    value = {
        "requests": 0,
        "tokens": 0,
        "seconds": 0,
        "reservations": {},
        "max_requests": 1,
        "max_tokens": 1,
        "max_seconds": 1,
    }
    atomic_write_json(path, {**value, **change})
    before = path.read_bytes()
    with pytest.raises(ValueError):
        FamilyBudget(path).reserve(
            RequestLimits.from_config({"processing": DEFAULT_PROCESSING}), 50, "facts", 10
        )
    assert path.read_bytes() == before


def test_recompilation_joins_original_allowance(kb_dir, tmp_path, model_service):
    import yaml

    from openkb.runtime.requests import ImportFile, RecompileDocument
    from openkb.runtime.tasks import TaskManager
    from openkb.sources import content_id

    config = kb_dir / ".openkb/config.yaml"
    settings = yaml.safe_load(config.read_text())
    settings["processing"]["max_requests"] = 4
    config.write_text(yaml.safe_dump(settings))
    original = tmp_path / "bounded.md"
    original.write_text("# Recovery\n\nRestart only after verifying the backup.")
    history = tmp_path / "history"
    manager = TaskManager(history_dir=history)
    try:
        first = manager.wait(manager.submit(kb_dir, [ImportFile(str(original))]), timeout=60)
        assert first.results[0].document.knowledge_compilation == "completed"
        manager.shutdown(stop=True)
        assert manager.join(10)
        manager = TaskManager(history_dir=history)
        registry = json.loads((kb_dir / ".openkb/hashes.json").read_text())
        key, metadata = next(iter(registry.items()))
        # A new model requires new work, but cannot grant a fresh task allowance.
        settings["model"] = "openai/changed-model"
        config.write_text(yaml.safe_dump(settings))
        task = manager.submit(kb_dir, [RecompileDocument(key, None, content_id(metadata))])
        result = manager.wait(task, timeout=60)
        assert result.results[0].document.reason == "request_budget_exhausted"
        assert len(model_service) == 4
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)
