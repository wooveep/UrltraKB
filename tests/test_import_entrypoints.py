"""Legacy adapters project the shared application import outcome (#73)."""

import importlib
import json
import shutil
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from openkb.application import documents


@pytest.fixture
def small_pdf(kb_dir):
    import pymupdf

    source = kb_dir / "raw" / "small.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "A fixed PDF for every entrypoint.")
        pdf.save(source)
    return source


@pytest.mark.parametrize("entrypoint", ["cli", "api", "watch"])
def test_pdf_adapters_use_shared_import_result(kb_dir, small_pdf, monkeypatch, entrypoint):
    cli = importlib.import_module("openkb.cli")
    source = small_pdf
    calls = []

    def import_document(root, path, **options):
        calls.append((root, path, options))
        return documents.DocumentResult(str(path), "skipped", ())

    monkeypatch.setattr(documents, "import_document", import_document)
    monkeypatch.setattr(cli, "_setup_llm_key", lambda _: None)
    monkeypatch.setattr(
        "litellm.completion", lambda **kwargs: pytest.fail("Application boundary was bypassed")
    )
    if entrypoint == "api":
        result = documents._add_for_api(source, kb_dir)
        assert result.status == "skipped"
        assert result.saved_path is None
    else:
        if entrypoint == "watch":
            monkeypatch.setattr(
                "openkb.watcher.watch_directory",
                lambda raw, callback, **kwargs: callback([str(source)]),
            )
            command = ["watch"]
        else:
            command = ["add", str(source)]
        result = CliRunner().invoke(cli.cli, ["--kb-dir", str(kb_dir), *command])
        assert result.exception is None, result.output
    assert [(root, path) for root, path, _ in calls] == [(kb_dir, source)]


def _application_boundary_worker(*args):
    from openkb.runtime.worker import run_unit

    def import_document(root, path, **options):
        assert path.read_bytes().startswith(b"%PDF-")
        return documents.DocumentResult(
            str(path), "skipped", (str(path),), input_version="fixed-pdf"
        )

    with patch.object(documents, "import_document", import_document):
        run_unit(*args)


def test_desktop_worker_projects_the_same_pdf_result(kb_dir, small_pdf, monkeypatch):
    from openkb.runtime.requests import ImportFile
    from openkb.runtime.tasks import TaskManager

    monkeypatch.setattr("openkb.runtime.tasks.run_unit", _application_boundary_worker)
    manager = TaskManager(history_dir=kb_dir / "task-history")
    try:
        task = manager.submit(kb_dir, [ImportFile(str(small_pdf))])
        result = manager.wait(task, timeout=20)
        assert result.skipped == 1
        assert result.results[0].resources == (str(small_pdf),)
        assert result.results[0].revision == "fixed-pdf"
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)


def test_invalid_text_preparation_is_a_failed_import(kb_dir):
    source = kb_dir / "raw" / "bad.md"
    source.write_bytes(b"\xff\xfe\x80")
    assert documents.add_single_file(source, kb_dir) == "failed"


def test_missing_batch_input_is_a_failed_import(kb_dir):
    assert documents.add_single_file(kb_dir / "missing.pdf", kb_dir) == "failed"


def test_replaced_kb_during_pdf_preparation_is_rejected(kb_dir, small_pdf, monkeypatch):
    from openkb.lifecycle import KnowledgeBaseRemoved

    copy = shutil.copy2
    external = kb_dir.with_name(kb_dir.name + ".pdf")
    copy(small_pdf, external)
    replaced = False

    def replacing_copy(source, target, **options):
        nonlocal replaced
        result = copy(source, target, **options)
        if not replaced:
            replaced = True
            previous = kb_dir.with_name(kb_dir.name + "-previous")
            kb_dir.rename(previous)
            shutil.copytree(previous, kb_dir)
        return result

    monkeypatch.setattr(shutil, "copy2", replacing_copy)
    with pytest.raises(KnowledgeBaseRemoved):
        documents.add_single_file(external, kb_dir)


def _model_responses():
    responses = iter(
        [
            {"description": "Fixed PDF", "content": "# Small\n\nCompiled knowledge."},
            {"create": [], "update": [], "related": []},
        ]
    )

    def completion(**kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(next(responses))))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
        )

    return completion


def _model_boundary_worker(*args):
    from openkb.runtime.worker import run_unit

    with patch("litellm.completion", _model_responses()):
        run_unit(*args)


@pytest.mark.parametrize("entrypoint", ["cli", "api", "desktop", "watch"])
def test_pdf_entrypoints_publish_the_same_knowledge(kb_dir, small_pdf, monkeypatch, entrypoint):
    from openkb.application.knowledge_bases import get_kb_list
    from openkb.application.pages import read_page
    from openkb.documents import read_document_source

    cli = importlib.import_module("openkb.cli")
    external = kb_dir / "input" / "small.pdf"
    external.parent.mkdir()
    shutil.move(small_pdf, external)
    monkeypatch.setattr("litellm.completion", _model_responses())
    if entrypoint == "desktop":
        from openkb.runtime.requests import ImportFile
        from openkb.runtime.tasks import TaskManager

        monkeypatch.setattr("openkb.runtime.tasks.run_unit", _model_boundary_worker)
        manager = TaskManager(history_dir=kb_dir / "task-history")
        try:
            task = manager.submit(kb_dir, [ImportFile(str(external))])
            assert manager.wait(task, timeout=20).succeeded == 1
        finally:
            manager.shutdown(stop=True)
            assert manager.join(10)
    elif entrypoint == "api":
        from fastapi.testclient import TestClient

        from openkb.api import create_app

        monkeypatch.setenv("OPENKB_KB_ROOT", str(kb_dir.parent))
        monkeypatch.delenv("OPENKB_API_TOKEN", raising=False)
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/add",
                data={"kb": kb_dir.name, "stream": "false"},
                files=[("files", ("small.pdf", external.read_bytes(), "application/pdf"))],
            )
        assert response.status_code == 200, response.text
        assert response.json()["added_count"] == 1
    else:
        if entrypoint == "watch":
            shutil.copy2(external, small_pdf)
            monkeypatch.setattr(
                "openkb.watcher.watch_directory",
                lambda raw, callback, **kwargs: callback([str(small_pdf)]),
            )
            command = ["watch"]
        else:
            command = ["add", str(external)]
        result = CliRunner().invoke(cli.cli, ["--kb-dir", str(kb_dir), *command])
        assert result.exception is None, result.output
    from openkb.application.views import view_scope

    document = get_kb_list(kb_dir)["documents"][0]
    scope = view_scope(kb_dir, document["view_id"])
    assert (
        read_page(kb_dir, "summaries/small", scope=scope).body.strip()
        == "# Small\n\nCompiled knowledge."
    )
    source = read_document_source(kb_dir, document["source_id"])
    assert "A fixed PDF for every entrypoint." in source["content"]
