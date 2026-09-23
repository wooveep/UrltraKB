"""Content omissions finish a normal import with an editable, honest source summary."""

import json

import pytest

from openkb.application.documents import import_document
from openkb.application.knowledge_bases import get_kb_list
from openkb.application.pages import read_page, save_page
from openkb.application.source_actions import continue_source, read_source_evidence
from openkb.evidence import Evidence
from tests.http_model_fixture import evidence_response


@pytest.mark.parametrize("text", ["", "![Unrecognized image](missing.png)"])
def test_no_readable_body_does_not_invent_knowledge(kb_dir, tmp_path, model_service, text):
    source = tmp_path / "no-body.md"
    source.write_text(text)
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert result.coverage["status"] == "partial"
    assert get_kb_list(kb_dir)["concepts"] == []
    assert not model_service


def test_pdf_without_recognized_text_publishes_original_and_zero_knowledge(
    kb_dir, tmp_path, model_service
):
    import pymupdf
    import yaml

    config_path = kb_dir / ".openkb/config.yaml"
    settings = yaml.safe_load(config_path.read_text())
    settings["navigation"] = {"enabled": True}
    config_path.write_text(yaml.safe_dump(settings))

    path = tmp_path / "illustration.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=160, height=160)
        page.draw_circle((80, 80), 30)
        pdf.save(path)
    result = import_document(kb_dir, path)
    assert result.status == "added", result
    assert result.coverage["status"] == "partial"
    inventory = get_kb_list(kb_dir)
    assert inventory["document_count"] == 1
    assert inventory["concepts"] == inventory["entities"] == []
    summary = read_page(kb_dir, "summaries/" + inventory["summaries"][0])
    assert "本次生成知识：0 条" in summary.body
    assert result.coverage["assets"]
    assert not model_service


@pytest.mark.parametrize("extension", ["docx", "pdf", "xlsx", "pptx", "txt"])
@pytest.mark.parametrize("packaged", [False, True])
def test_unreadable_original_is_registered_and_next_document_continues(
    kb_dir, tmp_path, model_service, extension, packaged
):
    path = tmp_path / ("broken." + extension)
    path.write_bytes(b"\x00unreadable\xff")
    if packaged:
        from zipfile import ZipFile

        with ZipFile(path, "w") as archive:
            archive.writestr("placeholder.txt", "Required document parts are missing.")
    result = import_document(kb_dir, path)
    assert result.status == "added", result
    assert result.coverage["status"] == "partial"
    assert get_kb_list(kb_dir)["document_count"] == 1
    assert not model_service
    following = tmp_path / "valid.txt"
    following.write_text("The metrics port is 9342.")
    assert import_document(kb_dir, following).status == "added"
    assert get_kb_list(kb_dir)["document_count"] == 2


@pytest.mark.parametrize("failed_stage", ["planning", "generation", "verification"])
def test_all_failed_content_registers_source_and_omissions(
    kb_dir, tmp_path, model_service, failed_stage
):
    original = tmp_path / "requirements.md"
    original.write_text("The required pressure is 37 kPa. Never retry authentication failure.")

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == failed_stage:
            return {"invalid": "No reliable output."}
        return evidence_response(payload)

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.status == "added", result
    assert result.knowledge_compilation == "completed"
    assert result.omissions and result.coverage["status"] == "partial"
    inventory = get_kb_list(kb_dir)
    assert inventory["document_count"] == 1
    assert inventory["concepts"] == inventory["entities"] == []
    summary = read_page(kb_dir, "summaries/" + inventory["summaries"][0])
    assert "本次生成知识：0 条" in summary.body
    assert "内容遗漏" in summary.body
    row = result.coverage["ranges"][0]
    reference = Evidence(result.source_id, result.input_version, result.parse_id, row["block_id"])
    assert read_source_evidence(kb_dir, reference, max_chars=1000).text == original.read_text()
    before = len(model_service)
    repeated = import_document(kb_dir, original)
    assert repeated.status == "skipped" and repeated.omissions == result.omissions
    assert repeated.coverage == result.coverage
    assert len(model_service) == before


def test_manual_completion_survives_repeat_and_continue_with_the_same_gap(
    kb_dir, tmp_path, model_service
):
    path = tmp_path / "missing.docx"
    from tests.document_fixtures import write_docx

    write_docx(path, "<w:p/>")
    first = import_document(kb_dir, path)
    summary_path = "summaries/" + get_kb_list(kb_dir)["summaries"][0]
    original = read_page(kb_dir, summary_path)
    manual = original.body + "\n\nManual correction: pressure is 37 kPa.\n"
    assert save_page(kb_dir, summary_path, manual, version=original.version).status == "saved"
    edited = read_page(kb_dir, summary_path)
    assert import_document(kb_dir, path).status == "skipped"
    continued = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert continued.knowledge_compilation == "completed", continued
    assert read_page(kb_dir, summary_path).content == edited.content
    assert continued.coverage["status"] == "partial"
    assert not model_service
    from openkb.application.source_cleanup import cleanup_history, preview_history_cleanup

    cleanup_history(kb_dir, preview_history_cleanup(kb_dir).id)
    resumed = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert resumed.knowledge_compilation == "completed", resumed
    assert read_page(kb_dir, summary_path).content == edited.content
    write_docx(path, "<w:p><w:r><w:t>The pressure must be 42 kPa.</w:t></w:r></w:p>")
    updated = import_document(kb_dir, path)
    assert updated.reason == "needs_acceptance", updated
    assert read_page(kb_dir, summary_path).content == edited.content


def test_invalid_docx_main_relationship_is_a_content_omission(kb_dir, tmp_path, model_service):
    from zipfile import ZipFile

    from tests.document_fixtures import write_docx

    path = tmp_path / "bad-relationship.docx"
    write_docx(path, "<w:p/>")
    with ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts["_rels/.rels"] = parts["_rels/.rels"].replace(b"word/document.xml", b"../document.xml")
    with ZipFile(path, "w") as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    result = import_document(kb_dir, path)
    assert result.status == "added", result
    assert result.coverage["status"] == "partial"
    assert not model_service
