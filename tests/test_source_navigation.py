"""Optional local navigation has independent limits and never controls knowledge completeness."""

import pytest
import yaml

from openkb.application.documents import import_document
from openkb.application.source_history import source_status
from openkb.evidence import ParseStore


def test_url_publication_receipt_and_history_are_settled_before_navigation(
    kb_dir, model_service, monkeypatch
):
    from openkb.application.execution import ExecutionContext
    from openkb.application.urls import import_url

    def fetch(url, root, **options):
        source = root / "article.md"
        source.write_text("Required version 7.")
        return source

    observed = []

    def committed(result):
        observed.append(result)
        raise SystemExit("Navigation worker lost after publication")

    monkeypatch.setattr("openkb.url_ingest.fetch_url_to_raw", fetch)
    with pytest.raises(SystemExit):
        import_url(
            kb_dir, "https://example.test/article", context=ExecutionContext(on_committed=committed)
        )
    assert len(observed) == 1
    result = observed[0]
    assert result.knowledge_compilation == "completed"
    assert result.usage["observable_attempts"] == 4
    saved = source_status(kb_dir, result.source_id)
    assert saved["result"]["knowledge_compilation"] == "completed"
    assert saved["cumulative_usage"]["observable_attempts"] == 4
    assert saved["cumulative_usage"]["charged_tokens"] == 520


def test_navigation_budget_failure_keeps_complete_knowledge_and_basic_positions(
    kb_dir, tmp_path, model_service
):
    path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(path.read_text())
    config["processing"]["max_requests"] = 4
    config["navigation"] = {
        "enabled": True,
        "processing": {
            **config["processing"],
            "max_requests": 1,
        },
    }
    path.write_text(yaml.safe_dump(config))
    source = tmp_path / "navigation.md"
    source.write_text(
        "# First\n\nRequired version 7.\n\n# Second\n\nNever retry authentication failures."
    )
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert "navigation_degraded" in result.warnings
    navigation = source_status(kb_dir, result.source_id)["navigation"]
    assert navigation["status"] == "degraded"
    assert navigation["reason"] == "request_budget_exhausted"
    assert {item["block_id"] for item in navigation["positions"]} == {
        block.id for block in ParseStore(kb_dir).load(result.parse_id).blocks
    }
    assert result.usage["observable_attempts"] == 4
    assert navigation["usage"]["observable_attempts"] == 1


def test_worker_loss_during_navigation_preserves_published_knowledge(
    kb_dir, tmp_path, model_service
):
    import json
    import multiprocessing
    import threading

    from openkb.runtime.requests import ImportFile
    from openkb.runtime.tasks import TaskManager
    from tests.http_model_fixture import evidence_response

    arrived, release = threading.Event(), threading.Event()

    def respond(body):
        try:
            payload = json.loads(body["messages"][-1]["content"])
        except ValueError:
            payload = {}
        value = evidence_response(payload)
        if value is not None:
            return value
        arrived.set()
        release.wait(15)
        return {"summary": "Navigation only"}

    model_service.respond = respond
    path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(path.read_text())
    config["navigation"] = {"enabled": True, "processing": config["processing"]}
    path.write_text(yaml.safe_dump(config))
    source = tmp_path / "navigation-loss.md"
    source.write_text("# First\n\nRequired version 7.")
    manager = TaskManager(history_dir=tmp_path / "history")
    task = manager.submit(kb_dir, [ImportFile(str(source))])
    try:
        assert arrived.wait(15), manager.get(task)
        assert (kb_dir / "wiki/concepts/notes.md").exists()
        for child in multiprocessing.active_children():
            if child.name.startswith(f"openkb-unit-{task[:8]}-"):
                child.terminate()
        result = manager.wait(task, timeout=15)
        assert result.processes_reaped
        assert result.succeeded == 1, result
        document = result.results[0].document
        assert document.knowledge_compilation == "completed"
        saved = source_status(kb_dir, document.source_id)
        assert saved["navigation"]["positions"]
        assert saved["navigation_usage"]["observable_attempts"] >= 1
        assert saved["navigation_usage"]["unknown_usage"] >= 1
        assert not saved["navigation_usage"]["accounting_complete"]
    finally:
        release.set()
        manager.shutdown(stop=True)
        assert manager.join(10)


def test_navigation_rebuild_uses_saved_parse_without_rewriting_knowledge(
    kb_dir, tmp_path, model_service
):
    from openkb.application.settings import apply_kb_config_patch, read_kb_config
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.application.source_actions import rebuild_source_navigation

    source = tmp_path / "rebuild-navigation.md"
    source.write_text("# First\n\nRequired version 7.\n\n# Second\n\nPressure 37 kPa.")
    imported = import_document(kb_dir, source)
    before = {p: p.read_bytes() for p in (kb_dir / "wiki").rglob("*") if p.is_file()}
    source.unlink()
    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["navigation"] = {"enabled": True, "processing": config["processing"]}
    apply_kb_config_patch(
        kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"navigation": config["navigation"]})
    )
    assert read_kb_config(kb_dir).sources["navigation"] == "kb"
    calls_before = len(model_service)
    rebuilt = rebuild_source_navigation(
        kb_dir, imported.source_id, version_id=imported.input_version, parse_id=imported.parse_id
    )
    assert rebuilt["status"] == "enhanced", rebuilt
    assert rebuilt["parse"] == imported.parse_id
    assert rebuilt["usage"]["observable_attempts"] == len(model_service) - calls_before
    assert rebuilt["usage"]["observable_attempts"] > 0
    assert {p: p.read_bytes() for p in (kb_dir / "wiki").rglob("*") if p.is_file()} == before
    for call in model_service[calls_before:]:
        assert '"stage": "facts"' not in call["messages"][-1]["content"]
        assert '"stage": "generation"' not in call["messages"][-1]["content"]
