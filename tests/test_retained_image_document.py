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


def test_retained_processing_preserves_prior_assets_when_resource_policy_changes(
    kb_dir, model_service, monkeypatch
):
    from pathlib import Path

    from figure_fixtures import png_bytes

    from openkb.application.documents import import_document
    from openkb.documents import read_document_source
    from openkb.markdown_images import image_references

    fetched = []

    def fetch(request, **kwargs):
        fetched.append(request.full_url)
        response = io.BytesIO(png_bytes("blue"))
        response.headers = {"Content-Type": "image/png"}
        return response

    monkeypatch.setattr("urllib.request.urlopen", fetch)
    source = kb_dir / "remote-only.html"
    source.write_text('<img src="https://example.test/figure.png" alt="Diagram">')
    first = import_document(kb_dir, source, download_remote_assets=False)
    old_entry = Path(first.resources[-1])
    old_content = old_entry.read_bytes()
    second = import_document(kb_dir, source, download_remote_assets=True)
    assert first.source_revision_id == second.source_revision_id
    assert first.units[0].target_revision_id != second.units[0].target_revision_id
    assert old_entry.read_bytes() == old_content
    viewed = read_document_source(kb_dir, first.source_id)
    assert len(image_references(viewed["content"])) == 1
    calls = len(fetched)
    third = import_document(kb_dir, source, download_remote_assets=True)
    assert third.units[0].target_revision_id == second.units[0].target_revision_id
    assert len(fetched) == calls
    assert old_entry.read_bytes() == old_content
    source.write_text('<img src="https://example.test/figure.png" alt="Updated diagram">')
    fourth = import_document(kb_dir, source, download_remote_assets=True)
    assert fourth.source_revision_id != first.source_revision_id
    historical = read_document_source(
        kb_dir, first.source_id, source_revision_id=first.source_revision_id
    )
    assert len(image_references(historical["content"])) == 1
    assert historical["unit_revision_id"] == second.units[0].target_revision_id
    assert old_entry.read_bytes() == old_content
    assert not model_service.requests


@pytest.mark.parametrize("suffix", [".md", ".html"])
def test_image_only_markup_retains_pixels_without_model_work(kb_dir, model_service, suffix):
    from openkb.application.documents import import_document
    from openkb.config import LlmCredentialBundle
    from openkb.documents import read_document_source
    from openkb.markdown_images import image_references

    source = kb_dir / f"diagram{suffix}"
    Image.new("RGB", (100, 100), "blue").save(kb_dir / "diagram.png")
    source.write_text(
        "![diagram](diagram.png)\n" if suffix == ".md" else '<img src="diagram.png" alt="diagram">'
    )
    result = import_document(
        kb_dir, source, bundle=LlmCredentialBundle(api_key="fixture", base_url=model_service.url)
    )
    assert result.status == "blocked", result.message
    viewed = read_document_source(kb_dir, result.source_id)
    refs = image_references(viewed["content"])
    assert len(refs) == 1
    with Image.open(kb_dir / viewed["base_path"] / refs[0].local_path) as image:
        assert image.getpixel((0, 0)) == (0, 0, 255)
    assert not model_service.requests
    assert not list((kb_dir / "wiki/summaries").glob("*.md"))


def test_retention_keeps_explicit_version_and_remote_asset_policy(
    kb_dir, model_service, monkeypatch
):
    from figure_fixtures import png_bytes

    from openkb.application.documents import import_document
    from openkb.documents import read_document_source
    from openkb.markdown_images import image_references
    from openkb.view_records import SourceMetadata

    fetched = []

    def fetch(request, **kwargs):
        fetched.append(request.full_url)
        result = io.BytesIO(png_bytes("blue"))
        result.headers = {"Content-Type": "image/png"}
        return result

    monkeypatch.setattr("urllib.request.urlopen", fetch)
    source = kb_dir / "versioned.html"
    source.write_text('<img src="https://example.test/figure.png" alt="Diagram">')
    result = import_document(
        kb_dir,
        source,
        download_remote_assets=True,
        metadata=SourceMetadata(product="Device", applicable_versions=("1",)),
    )
    assert result.status == "blocked", result.message
    viewed = read_document_source(kb_dir, result.source_id)
    assert viewed["view_id"] != "legacy"
    assert viewed["version_metadata"]["product"] == "Device"
    assert fetched == ["https://example.test/figure.png"]
    assert len(image_references(viewed["content"])) == 1
    assert not model_service.requests


def test_image_only_replacement_keeps_previous_success_and_explicit_new_original(
    kb_dir, monkeypatch
):
    from test_compile_windows import compile_boundary

    from openkb.application.documents import import_document
    from openkb.documents import read_document_source
    from openkb.unit_publication import read_head

    source = kb_dir / "guide.md"
    Image.new("RGB", (32, 32), "blue").save(kb_dir / "diagram.png")
    source.write_text("# Setup\nDisconnect before service.\n![Figure](diagram.png)")
    calls = []
    compile_boundary(monkeypatch, {}, calls)
    first = import_document(kb_dir, source)
    assert first.status == "added"
    head = read_head(kb_dir)
    previous = (kb_dir / "wiki/summaries/guide.md").read_bytes()
    calls.clear()
    source.write_text("![Figure](diagram.png)")
    replacement = import_document(kb_dir, source)
    assert replacement.status == "blocked"
    assert replacement.source_id == first.source_id
    assert replacement.units[0].successful_revision_id == first.units[0].successful_revision_id
    assert read_head(kb_dir).knowledge_revision_id == head.knowledge_revision_id
    assert (kb_dir / "wiki/summaries/guide.md").read_bytes() == previous
    assert "Disconnect" in read_document_source(kb_dir, first.source_id)["content"]
    latest = read_document_source(
        kb_dir, first.source_id, source_revision_id=replacement.source_revision_id
    )
    assert "未生成知识" in latest["content"] and "本节原图" in latest["content"]
    assert not calls
