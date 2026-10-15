"""A binary PowerPoint file retains the same verified slide and notes contract."""

import shutil
from pathlib import Path

import pytest

pytest_plugins = ("test_presentation_import",)


@pytest.mark.parametrize("limit", [10, 1])
def test_binary_ppt_keeps_hidden_page_notes_and_offline_recompilation(
    kb_dir, tmp_path, office_runtime, presentation_model, live_llm, limit
):
    import asyncio

    from publication_fixtures import publish_document

    from ultrakb.application.documents import import_document
    from ultrakb.application.recompilation import recompile_document, select_recompilation
    from ultrakb.application.settings import apply_kb_config_patch
    from ultrakb.application.settings_data import KbConfigPatchRequest
    from ultrakb.documents import read_document_source

    apply_kb_config_patch(
        kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"pdf_short_max_pages": limit})
    )
    source = tmp_path / "旧幻灯片.ppt"
    shutil.copyfile(Path(__file__).parent / "fixtures/office/slides.ppt", source)
    # Short-mode setup exercises the real publication boundary with the exact
    # normalized source. The offline recompile below generates genuine pages;
    # segmented mode also keeps its real index/compile preparation chain.
    imported = (
        publish_document(kb_dir, source, None, bundle=live_llm.credentials)
        if limit == 10
        else import_document(kb_dir, source)
    )
    assert imported.status == "added", imported.message
    saved = read_document_source(kb_dir, imported.source_id)
    assert saved["pages"] == 3
    assert saved["office"]["detected_filter"] == "MS PowerPoint 97"
    assert saved["office"]["slides"][1]["hidden"]
    assert "HIDDEN_SLIDE_BODY" in saved["units"][1]["parts"]["body"]
    assert saved["units"][1]["parts"]["notes"] == "Hidden speaker note"
    assert saved["processing"]["pdf_limit"]["short_max_pages"] == limit
    assert (kb_dir / saved["original_path"]).read_bytes() == source.read_bytes()
    source.unlink()
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir),
            config={
                "office_runtime_path": str(tmp_path / "gone"),
                "pdf_short_max_pages": 0,
            },
        ),
    )
    selected = select_recompilation(kb_dir, imported.source_id, confirmation=True)
    result = asyncio.run(recompile_document(kb_dir, imported.source_id, version=selected.version))
    assert result.status == "compiled", result.message
    again = read_document_source(kb_dir, imported.source_id)
    assert again["office"] == saved["office"]
    assert again["content"] == saved["content"]
    assert again["processing"] == saved["processing"]
    assert again["source_revision_id"] == saved["source_revision_id"]


def test_unreadable_ppt_update_retains_old_slide_evidence(
    kb_dir, tmp_path, office_runtime, live_llm
):
    from publication_fixtures import publish_document

    from ultrakb.application.documents import import_document
    from ultrakb.documents import read_document_source

    source = tmp_path / "slides.ppt"
    shutil.copyfile(Path(__file__).parent / "fixtures/office/slides.ppt", source)
    first = publish_document(kb_dir, source, None, bundle=live_llm.credentials)
    assert first.status == "added", first.message
    previous = read_document_source(kb_dir, first.source_id)
    source.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1truncated")
    failed = import_document(kb_dir, source)
    assert failed.status == "failed" and "compound-file header is incomplete" in failed.message
    actual = read_document_source(kb_dir, first.source_id)
    assert actual["knowledge_revision_id"] == previous["knowledge_revision_id"]
    assert actual["content"] == previous["content"]
    assert actual["office"] == previous["office"]


def test_ppt_upload_and_cli_read_use_the_same_physical_slides(kb_dir, office_runtime):
    import asyncio
    import io
    import json

    from click.testing import CliRunner
    from fastapi import UploadFile
    from fastapi.testclient import TestClient
    from publication_fixtures import publish_document

    from ultrakb.api import create_app
    from ultrakb.api_helpers import _cleanup_uploads, _reserve_add_uploads, _write_add_uploads
    from ultrakb.application.uploads import published_input
    from ultrakb.cli import cli
    from ultrakb.config import LlmCredentialBundle, register_kb_alias
    from ultrakb.documents import read_document_source
    from ultrakb.source_catalog import read_source

    original = (Path(__file__).parent / "fixtures/office/slides.ppt").read_bytes()
    upload = UploadFile(filename="legacy.ppt", file=io.BytesIO(original))
    reserved = _reserve_add_uploads(kb_dir, [upload])
    ready = asyncio.run(_write_add_uploads(reserved, [upload]))
    try:
        # This regression verifies actual upload ownership and native source
        # reading. Compiler delivery is covered by the genuine offline
        # recompilation cases above; domain preparation sends no model request.
        with published_input(kb_dir, ready[0][0]) as owned:
            imported = publish_document(
                kb_dir, owned.path, None, bundle=LlmCredentialBundle(api_key="local-ppt-upload")
            )
    finally:
        _cleanup_uploads(ready)
    assert imported.status == "added", imported.message
    admitted = read_source(kb_dir, imported.source_id)
    assert admitted.identity == owned.path.relative_to(kb_dir).as_posix()
    assert admitted.target_revision_id == imported.source_revision_id
    full = read_document_source(kb_dir, imported.source_id)
    assert (kb_dir / full["original_path"]).read_bytes() == original
    assert full["pages"] == 3
    assert full["office"]["slides"][1]["hidden"]
    assert "HIDDEN_SLIDE_BODY" in full["units"][1]["parts"]["body"]
    assert full["units"][1]["parts"]["notes"] == "Hidden speaker note"
    [picture] = full["units"][1]["images"]
    assert (kb_dir / full["base_path"] / picture["path"]).is_file()

    register_kb_alias("old-slides", kb_dir)
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/document/source",
            json={
                "kb": "old-slides",
                "hash": imported.source_id,
                "pages": "3",
                "part": "notes",
            },
        )
    assert response.status_code == 200, response.text
    notes = response.json()
    assert notes["pages"] == 3
    assert notes["content"] == "[Physical page 3 · 演讲备注]\n\nNotes-only page evidence."
    assert notes["units"][0]["parts"]["notes"] == "Notes-only page evidence."
    output = CliRunner().invoke(
        cli,
        ["--kb-dir", str(kb_dir), "source", imported.source_id, "--pages", "3", "--part", "notes"],
    )
    assert output.exit_code == 0, output.output
    saved = json.loads(output.output)
    assert saved["pages"] == 3 and "Notes-only page evidence." in saved["content"]
    for field in (
        "source_id",
        "source_revision_id",
        "knowledge_revision_id",
        "content",
        "pages",
        "part",
        "units",
        "office",
        "original_path",
        "processing",
        "target_processing",
    ):
        assert saved[field] == notes[field]
