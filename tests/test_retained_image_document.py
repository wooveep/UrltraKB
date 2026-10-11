"""A document without usable native text remains readable without model work."""

import io

import pymupdf
import pytest
from PIL import Image


@pytest.mark.parametrize("entrypoint", ["shared", "cli", "http", "desktop"])
def test_scan_only_pdf_is_retained_without_knowledge_or_model_sends(
    kb_dir, model_service, monkeypatch, entrypoint
):
    from openkb.application.documents import import_document
    from openkb.application.execution import ExecutionContext
    from openkb.config import LlmCredentialBundle
    from openkb.markdown_images import image_references
    from openkb.source_catalog import read_source_revision

    data = io.BytesIO()
    Image.new("RGB", (100, 100), "green").save(data, format="PNG")
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_image(pymupdf.Rect(0, 0, 400, 400), stream=data.getvalue())
    source = kb_dir / "scan.pdf"
    pdf.save(source)
    pdf.close()
    if entrypoint == "shared":
        result = import_document(
            kb_dir,
            source,
            context=ExecutionContext(),
            bundle=LlmCredentialBundle(api_key="fixture", base_url=model_service.url),
        )
        assert result.status == "blocked"
        assert "资料已保存，未生成知识" in result.message
    elif entrypoint == "cli":
        from click.testing import CliRunner

        from openkb.cli import cli

        result = CliRunner().invoke(cli, ["--kb-dir", str(kb_dir), "add", str(source)])
        assert result.exception is None, result.output
        assert "资料已保存，未生成知识" in result.output
    elif entrypoint == "http":
        from fastapi.testclient import TestClient

        from openkb.api import create_app

        monkeypatch.setenv("OPENKB_KB_ROOT", str(kb_dir.parent))
        monkeypatch.delenv("OPENKB_API_TOKEN", raising=False)
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/add",
                data={"kb": kb_dir.name, "stream": "false"},
                files=[("files", ("scan.pdf", source.read_bytes(), "application/pdf"))],
            )
            assert response.status_code == 200, response.text
            assert "资料已保存，未生成知识" in response.text
    else:
        from openkb.runtime.requests import ImportFile
        from openkb.runtime.tasks import TaskManager

        manager = TaskManager(history_dir=kb_dir / "task-history")
        try:
            task = manager.submit(kb_dir, [ImportFile(str(source))])
            result = manager.wait(task, timeout=20)
            assert result.results[0].status == "blocked"
        finally:
            manager.shutdown(stop=True)
            assert manager.join(10)
    from openkb.documents import read_document_source
    from openkb.source_catalog import list_sources

    admitted = list_sources(kb_dir)[0]
    saved = read_source_revision(kb_dir, admitted.target_revision_id)
    assert (kb_dir / saved.original).read_bytes() == source.read_bytes()
    viewed = read_document_source(kb_dir, admitted.source_id, pages="1")
    assert viewed["status"] == "blocked"
    assert "资料已保存，未生成知识" in viewed["content"]
    refs = image_references(viewed["content"])
    assert len(refs) == 1
    assert (kb_dir / viewed["base_path"] / refs[0].local_path).is_file()
    assert not list((kb_dir / "wiki/summaries").glob("scan.md"))
    assert not model_service.requests


pytest_plugins = ("test_vendor_sdk",)
