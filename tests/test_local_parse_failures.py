"""Recoverable source omissions must preserve useful work and immutable caches."""

import io
from zipfile import ZipFile

import pytest

from openkb.application.documents import import_document
from openkb.evidence import ParseStore
from openkb.inputs import prepared_input
from openkb.parsing import parse_document
from openkb.sources import SourceStore
from tests.document_fixtures import write_docx
from tests.docx_attachment_fixtures import (
    attached_docx,
    compound_file,
    docx_with_parts,
    native_package,
)


def _broken_child(tmp_path):
    child = tmp_path / "child.docx"
    write_docx(child, "<w:p><w:r><w:t>Child instruction.</w:t></w:r></w:p>")
    with ZipFile(child) as archive:
        parts = {n: archive.read(n) for n in archive.namelist()}
    parts["word/document.xml"] = b"<broken"
    stream = io.BytesIO()
    with ZipFile(stream, "w") as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return stream.getvalue()


def test_broken_embedded_document_does_not_prevent_body_publication(
    kb_dir, tmp_path, model_service
):
    parent = attached_docx(tmp_path / "parent.docx", _broken_child(tmp_path))
    result = import_document(kb_dir, parent)
    assert result.knowledge_compilation == "completed", result
    assert any("docx_attachment_unparsed:" in warning for warning in result.warnings)
    parsed = ParseStore(kb_dir).load(result.parse_id)
    assert any(row["status"] == "needs_review" for row in parsed.quality)
    assert any(
        "无法解析" in p.read_text(encoding="utf-8")
        for p in (kb_dir / "wiki/summaries").glob("*.md")
    )
    assert list((kb_dir / "wiki/sources/attachments").glob("*.docx"))


@pytest.mark.parametrize("broken", [False, True])
def test_continuation_reuses_embedded_extraction_even_with_local_warnings(
    kb_dir, tmp_path, monkeypatch, broken
):
    import openkb.parsing_docx as docx

    child = tmp_path / "child.docx"
    write_docx(child, "<w:p><w:r><w:t>Child instruction.</w:t></w:r></w:p>")
    payload = _broken_child(tmp_path) if broken else child.read_bytes()
    parent = attached_docx(tmp_path / "parent.docx", payload)
    with prepared_input(parent) as ready:
        source = SourceStore(kb_dir).intake(ready)
    first = parse_document(kb_dir, source)

    def unexpected_unpack(*args, **kwargs):
        pytest.fail("Ordinary continuation must not unpack the DOCX or embedded objects again")

    monkeypatch.setattr(docx, "prepare_docx", unexpected_unpack)
    second = parse_document(kb_dir, source)
    assert second.id == first.id
    assert len(SourceStore(kb_dir).list_sources()) == 2


def test_unknown_word_element_is_visible_but_does_not_block_body(kb_dir, tmp_path, model_service):
    path = tmp_path / "unsupported.docx"
    write_docx(
        path,
        "<w:p><w:r><w:t>Timeout is 42 seconds.</w:t>"
        "<w:unsupported><w:t>Unrecognized object.</w:t></w:unsupported></w:r></w:p>",
    )
    result = import_document(kb_dir, path)
    assert result.knowledge_compilation == "completed", result
    assert any("docx_conversion_warning:" in warning for warning in result.warnings)
    assert any(
        "无法解析" in p.read_text(encoding="utf-8")
        for p in (kb_dir / "wiki/summaries").glob("*.md")
    )


@pytest.mark.parametrize("extension", ["pdf", "xlsx", "pptx", "txt"])
def test_unreadable_document_formats_are_isolated(kb_dir, tmp_path, model_service, extension):
    parent = attached_docx(tmp_path / "parent.docx", _broken_child(tmp_path))
    with ZipFile(parent) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts["word/embeddings/object.bin"] = compound_file(
        "\x01Ole10Native", native_package("broken." + extension, b"\x00unreadable\xff" * 600)
    )
    with ZipFile(parent, "w") as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    result = import_document(kb_dir, parent)
    assert result.knowledge_compilation == "completed", result
    assert any("docx_attachment_unparsed:" in warning for warning in result.warnings)
    assert list((kb_dir / "wiki/sources/attachments").glob("*." + extension))


def test_corrupt_optional_word_part_keeps_body(kb_dir, tmp_path, model_service):
    path = docx_with_parts(
        tmp_path / "bad-comments.docx",
        "<w:p><w:r><w:t>Timeout is 42 seconds.</w:t></w:r></w:p>",
        parts={"word/comments.xml": b"<broken"},
        relationships='<Relationship Id="comments" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" '
        'Target="comments.xml"/>',
    )
    result = import_document(kb_dir, path)
    assert result.knowledge_compilation == "completed", result
    assert "docx_part_unparsed:word/comments.xml" in result.warnings


def test_force_reparse_refreshes_body_but_reuses_unchanged_child(kb_dir, tmp_path, monkeypatch):
    import openkb.parsing_docx as docx

    child = tmp_path / "child.docx"
    write_docx(child, "<w:p><w:r><w:t>Child instruction.</w:t></w:r></w:p>")
    parent = attached_docx(tmp_path / "parent.docx", child.read_bytes())
    with prepared_input(parent) as ready:
        source = SourceStore(kb_dir).intake(ready)
    first = parse_document(kb_dir, source)
    calls = []
    original = docx.prepare_docx

    def count(*args):
        calls.append(args[-1])
        return original(*args)

    monkeypatch.setattr(docx, "prepare_docx", count)
    assert parse_document(kb_dir, source, force=True).id == first.id
    assert calls == [0]


def test_unusable_whole_document_is_not_published(kb_dir, tmp_path, model_service):
    path = tmp_path / "empty.docx"
    write_docx(path, "<w:p/>")
    result = import_document(kb_dir, path)
    assert result.knowledge_compilation != "completed"
    assert not model_service


@pytest.mark.parametrize("failure", ["deadline", "storage"])
def test_local_omission_handling_does_not_swallow_execution_failures(
    kb_dir, tmp_path, model_service, monkeypatch, failure
):
    from openkb.processing import ProcessingIncomplete

    child = tmp_path / "child.docx"
    write_docx(child, "<w:p><w:r><w:t>Child instruction.</w:t></w:r></w:p>")
    parent = attached_docx(tmp_path / "parent.docx", child.read_bytes())

    def fail(*args, **kwargs):
        if failure == "deadline":
            raise ProcessingIncomplete("document_time_budget_exceeded", "parsing")
        raise OSError("Storage unavailable")

    monkeypatch.setattr("openkb.docx_attachments.parse_attachment", fail)
    result = import_document(kb_dir, parent)
    assert result.knowledge_compilation != "completed"
    assert not model_service


def test_missing_footnote_keeps_following_body(kb_dir, tmp_path, model_service):
    path = tmp_path / "missing-note.docx"
    write_docx(
        path,
        '<w:p><w:r><w:footnoteReference w:id="99"/></w:r></w:p>'
        "<w:p><w:r><w:t>Following instruction: timeout 42.</w:t></w:r></w:p>",
    )
    result = import_document(kb_dir, path)
    assert result.knowledge_compilation == "completed", result
    assert "unresolved_docx_note" in result.warnings
    parsed = ParseStore(kb_dir).load(result.parse_id)
    store = SourceStore(kb_dir)
    assert any(
        "Following instruction" in store.asset(block.blob).read_text() for block in parsed.blocks
    )


def test_missing_note_marker_alone_is_not_usable_content(kb_dir, tmp_path, model_service):
    path = tmp_path / "only-missing-note.docx"
    write_docx(path, '<w:p><w:r><w:footnoteReference w:id="99"/></w:r></w:p>')
    result = import_document(kb_dir, path)
    assert result.knowledge_compilation != "completed"
    assert not model_service
