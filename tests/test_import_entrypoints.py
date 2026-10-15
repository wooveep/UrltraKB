"""Legacy adapters project the shared application import outcome (#73)."""

import importlib
import shutil
from unittest.mock import patch

import pytest
from application_llm_fixtures import real_application_model  # noqa: F401
from click.testing import CliRunner

from ultrakb.application import documents


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
    cli = importlib.import_module("ultrakb.cli")
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
                "ultrakb.watcher.watch_directory",
                lambda raw, callback, **kwargs: callback([str(source)]),
            )
            command = ["watch"]
        else:
            command = ["add", str(source)]
        result = CliRunner().invoke(cli.cli, ["--kb-dir", str(kb_dir), *command])
        assert result.exception is None, result.output
    assert [(root, path) for root, path, _ in calls] == [(kb_dir, source)]


def _application_boundary_worker(*args):
    from ultrakb.runtime.worker import run_unit

    def import_document(root, path, **options):
        assert path.read_bytes().startswith(b"%PDF-")
        return documents.DocumentResult(
            str(path), "skipped", (str(path),), input_version="fixed-pdf"
        )

    with patch.object(documents, "import_document", import_document):
        run_unit(*args)


def test_desktop_worker_projects_the_same_pdf_result(kb_dir, small_pdf, monkeypatch):
    from ultrakb.runtime.requests import ImportFile
    from ultrakb.runtime.tasks import TaskManager

    monkeypatch.setattr("ultrakb.runtime.tasks.run_unit", _application_boundary_worker)
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
    from ultrakb.lifecycle import KnowledgeBaseRemoved

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


@pytest.mark.parametrize("entrypoint", ["cli", "api", "desktop", "watch"])
def test_pdf_entrypoints_publish_the_same_knowledge(kb_dir, small_pdf, monkeypatch, entrypoint):
    from ultrakb.application.knowledge_bases import get_kb_list
    from ultrakb.application.pages import read_page
    from ultrakb.documents import read_document_source

    cli = importlib.import_module("ultrakb.cli")
    external = kb_dir / "input" / "small.pdf"
    external.parent.mkdir()
    shutil.move(small_pdf, external)
    if entrypoint == "desktop":
        from ultrakb.runtime.requests import ImportFile
        from ultrakb.runtime.tasks import TaskManager

        manager = TaskManager(history_dir=kb_dir / "task-history")
        try:
            task = manager.submit(kb_dir, [ImportFile(str(external))])
            assert manager.wait(task, timeout=120).succeeded == 1
        finally:
            manager.shutdown(stop=True)
            assert manager.join(10)
    elif entrypoint == "api":
        from fastapi.testclient import TestClient

        from ultrakb.api import create_app

        monkeypatch.setenv("ULTRAKB_KB_ROOT", str(kb_dir.parent))
        monkeypatch.delenv("ULTRAKB_API_TOKEN", raising=False)
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
                "ultrakb.watcher.watch_directory",
                lambda raw, callback, **kwargs: callback([str(small_pdf)]),
            )
            command = ["watch"]
        else:
            command = ["add", str(external)]
        result = CliRunner().invoke(cli.cli, ["--kb-dir", str(kb_dir), *command])
        assert result.exception is None, result.output
    from ultrakb.application.views import view_scope

    document = get_kb_list(kb_dir)["documents"][0]
    scope = view_scope(kb_dir, document["view_id"])
    body = read_page(kb_dir, "summaries/small", scope=scope).body.lower()
    assert "fixed" in body and "pdf" in body and "entry" in body
    source = read_document_source(kb_dir, document["source_id"])
    assert "A fixed PDF for every entrypoint." in source["content"]
