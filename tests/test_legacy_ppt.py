"""A binary PowerPoint file retains the same verified slide and notes contract."""

import shutil
from pathlib import Path

import pytest

pytest_plugins = ("test_presentation_import",)


@pytest.mark.parametrize("limit", [10, 1])
def test_binary_ppt_keeps_hidden_page_notes_and_offline_recompilation(
    kb_dir, tmp_path, office_runtime, presentation_model, limit
):
    import asyncio

    from openkb.application.documents import import_document
    from openkb.application.recompilation import recompile_document, select_recompilation
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.documents import read_document_source

    apply_kb_config_patch(
        kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"pdf_short_max_pages": limit})
    )
    source = tmp_path / "旧幻灯片.ppt"
    shutil.copyfile(Path(__file__).parent / "fixtures/office/slides.ppt", source)
    imported = import_document(kb_dir, source)
    assert imported.status == "added", imported.message
    saved = read_document_source(kb_dir, imported.source_id)
    assert saved["pages"] == 3
    assert saved["office"]["detected_filter"] == "MS PowerPoint 97"
    assert saved["office"]["slides"][1]["hidden"]
    assert "HIDDEN_SLIDE_BODY" in saved["units"][1]["parts"]["body"]
    assert saved["units"][1]["parts"]["notes"] == "Hidden speaker note"
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


def test_unreadable_ppt_update_retains_old_slide_evidence(
    kb_dir, tmp_path, office_runtime, pdf_model
):
    from openkb.application.documents import import_document
    from openkb.documents import read_document_source

    source = tmp_path / "slides.ppt"
    shutil.copyfile(Path(__file__).parent / "fixtures/office/slides.ppt", source)
    first = import_document(kb_dir, source)
    assert first.status == "added", first.message
    previous = read_document_source(kb_dir, first.source_id)
    source.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1truncated")
    failed = import_document(kb_dir, source)
    assert failed.status == "failed" and "compound-file header is incomplete" in failed.message
    actual = read_document_source(kb_dir, first.source_id)
    assert actual["knowledge_revision_id"] == previous["knowledge_revision_id"]
    assert actual["content"] == previous["content"]
    assert actual["office"] == previous["office"]


def test_ppt_upload_and_cli_read_use_the_same_physical_slides(kb_dir, office_runtime, pdf_model):
    import json

    from click.testing import CliRunner
    from fastapi.testclient import TestClient

    from openkb.api import create_app
    from openkb.cli import cli
    from openkb.config import register_kb_alias

    register_kb_alias("old-slides", kb_dir)
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/add",
            data={"kb": "old-slides", "stream": "false"},
            files={
                "files": (
                    "legacy.ppt",
                    (Path(__file__).parent / "fixtures/office/slides.ppt").read_bytes(),
                )
            },
        )
    assert response.status_code == 200, response.text
    result = response.json()["files"][0]
    assert result["status"] == "added", result
    output = CliRunner().invoke(
        cli,
        ["--kb-dir", str(kb_dir), "source", result["source_id"], "--pages", "3", "--part", "notes"],
    )
    assert output.exit_code == 0, output.output
    saved = json.loads(output.output)
    assert saved["pages"] == 3 and "Notes-only page evidence." in saved["content"]
