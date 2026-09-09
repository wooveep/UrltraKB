"""URL acquisition is private preparation; the shared ingest owns publication."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openkb.application.urls import import_url


def test_url_compiles_records_provenance_and_deduplicates(kb_dir, monkeypatch):
    import litellm

    from openkb.application.execution import ExecutionContext
    from openkb.locks import kb_ingest_lock_held
    from openkb.state import HashRegistry

    context = ExecutionContext()
    private = []

    def fetch(url, root, **options):
        assert context.snapshot is not None
        assert kb_ingest_lock_held(kb_dir / ".openkb")
        target = root / "raw/article.md"
        target.parent.mkdir()
        target.write_text("# Article\nOriginal content.")
        private.append(target)
        options["on_quality"]("short_extraction")
        return target

    values = iter(
        [
            {"description": "Article", "content": "# Article\nCompiled."},
            {"create": [], "update": [], "related": []},
        ]
    )
    monkeypatch.setattr(
        litellm,
        "completion",
        lambda **kwargs: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(next(values))))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        ),
    )
    url = "https://example.com/article"
    with patch("openkb.url_ingest.fetch_url_to_raw", side_effect=fetch):
        result = import_url(kb_dir, url, context=context)
        duplicate = import_url(kb_dir, url, context=context)
    assert result.source == url and result.status == "added"
    assert result.quality == ("short_extraction",)
    assert result.resources and all(Path(path).exists() for path in result.resources)
    assert duplicate.status == "skipped"
    assert all(not path.exists() for path in private)
    entry = next(iter(HashRegistry(kb_dir / ".openkb/hashes.json").all_entries().values()))
    assert entry["origin"] == "url" and entry["path"] == url


def test_failed_fetch_uses_fixed_snapshot_without_publishing_raw(kb_dir):
    from openkb.application.execution import ExecutionContext

    context = ExecutionContext()
    with patch("openkb.url_ingest.fetch_url_to_raw", return_value=None):
        result = import_url(kb_dir, "https://example.com/missing", context=context)
    assert result.status == "failed" and result.unfinished == ("acquisition",)
    assert result.resources == () and context.snapshot is not None
    assert not list((kb_dir / "raw").iterdir())


def test_url_acquisition_consumes_document_budget_without_starting_compilation(kb_dir):
    import time

    import yaml

    config_path = kb_dir / ".openkb/config.yaml"
    settings = yaml.safe_load(config_path.read_text())
    settings["processing"]["document_timeout"] = 0.02
    config_path.write_text(yaml.safe_dump(settings))

    def slow_download(url, root, **options):
        time.sleep(0.03)
        options["cancelled"]()
        raise AssertionError("Download must stop when its document budget expires")

    with (
        patch("openkb.url_ingest.fetch_url_to_raw", side_effect=slow_download),
        patch("openkb.application.urls.import_document") as compile_document,
    ):
        result = import_url(kb_dir, "https://example.com/slow")
    assert result.status == "unfinished" and result.stage == "acquiring"
    assert result.reason == "time_budget_exhausted"
    assert result.source_intake == "not_saved"
    assert result.usage["observable_attempts"] == 0
    compile_document.assert_not_called()


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http:///missing-host", "not a URL"])
def test_url_boundary_rejects_non_http_inputs(kb_dir, url):
    with pytest.raises(ValueError):
        import_url(kb_dir, url)


def test_deferred_url_uses_the_same_complete_private_input(kb_dir, tmp_path):
    from openkb.application.documents import DocumentResult
    from openkb.runtime.worker import WaitingForLease

    def fetch(url, root, **options):
        target = root / "raw/one-shot.md"
        target.parent.mkdir()
        target.write_text("One-time input")
        return target

    preparation = tmp_path / "prepared"
    preparation.mkdir()
    with (
        patch("openkb.url_ingest.fetch_url_to_raw", side_effect=fetch) as acquisition,
        patch(
            "openkb.application.urls.import_document",
            side_effect=[WaitingForLease(), DocumentResult("", "added", ())],
        ) as ingest,
    ):
        with pytest.raises(WaitingForLease):
            import_url(kb_dir, "https://example.com/once", prepared_dir=preparation)
        result = import_url(kb_dir, "https://example.com/once", prepared_dir=preparation)
    assert result.status == "added" and acquisition.call_count == 1
    assert ingest.call_args_list[0].args[1] == ingest.call_args_list[1].args[1]


def test_pdf_declared_length_must_be_received(tmp_path):
    from io import BytesIO

    from openkb.url_ingest import _download_pdf_chunked

    response = BytesIO(b"partial")
    response.headers = {"Content-Length": "10000"}
    with pytest.raises(OSError):
        _download_pdf_chunked(response, b"%PDF-", tmp_path / "incomplete.pdf")


def test_spawn_failure_discards_prepared_input_for_that_task(kb_dir, tmp_path):
    from unittest.mock import MagicMock

    from openkb.runtime.records import TaskView, UnitIdentity
    from openkb.runtime.requests import ImportUrl
    from openkb.runtime.tasks import TaskManager, _Task

    manager = TaskManager(history_dir=tmp_path / "history")
    request = ImportUrl("https://example.com/once")
    task_id = "a" * 32
    identity = UnitIdentity.create(task_id, 0, str(kb_dir), request)
    task = _Task(
        TaskView(task_id, str(kb_dir), "ImportUrl", "waiting", "waiting", 1, (), False, True),
        (request,),
        (identity,),
    )
    prepared = Path(manager._preparations.name) / task_id / identity.unit_id
    prepared.mkdir(parents=True)
    cached = prepared / "input.pdf"
    cached.write_bytes(b"downloaded")
    process = MagicMock()
    process.start.side_effect = OSError("spawn failed")
    try:
        with manager._condition:
            manager._tasks[task_id] = task
            with patch.object(manager._context, "Process", return_value=process):
                manager._start(task)
        assert manager.get(task_id).state == "failed" and not cached.exists()
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)
