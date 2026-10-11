"""Native PDF layout changes require explicit new processing revisions."""

import json
from types import SimpleNamespace

import pytest

pytest_plugins = ("test_pdf_readback",)


@pytest.mark.parametrize("source_format", ["pdf", "docx", "pptx", "caj"])
def test_pdf_based_normalization_identity_includes_native_layout_policy(
    kb_dir, monkeypatch, source_format
):
    from openkb.normalization import normalization_fingerprint, normalization_id

    monkeypatch.setattr(
        "openkb.office.runtime.processing_identity", lambda *_: {"fixture": "office"}
    )
    monkeypatch.setattr("openkb.cnki.runtime.processing_identity", lambda: {"fixture": "cnki"})
    revision = SimpleNamespace(source_format=source_format)
    current = normalization_fingerprint(kb_dir, source_revision=revision)
    monkeypatch.setattr("openkb.pdf_layout.PDF_LAYOUT_POLICY", "previous-native-layout")
    previous = normalization_fingerprint(kb_dir, source_revision=revision)

    assert normalization_id("1" * 32, current) != normalization_id("1" * 32, previous)
    assert json.loads(current)["pdf_layout_policy"] != json.loads(previous)["pdf_layout_policy"]


def test_native_text_normalization_does_not_change_with_pdf_layout_policy(kb_dir, monkeypatch):
    from openkb.normalization import normalization_fingerprint

    revision = SimpleNamespace(source_format="md", digest="a" * 64, text_decoding=None, assets=[])
    current = normalization_fingerprint(kb_dir, source_revision=revision, doc_name="note")
    monkeypatch.setattr("openkb.pdf_layout.PDF_LAYOUT_POLICY", "previous-native-layout")
    assert normalization_fingerprint(kb_dir, source_revision=revision, doc_name="note") == current


def test_recompile_retains_old_projection_and_reprocess_creates_immutable_new_version(
    kb_dir, tmp_path, pdf_model, monkeypatch
):
    import asyncio

    from test_pdf_tables import merged_table_pdf

    from openkb.application.documents import import_document
    from openkb.application.recompilation import recompile_document
    from openkb.application.reprocessing import preview_reprocessing, reprocess_source
    from openkb.documents import read_document_source
    from openkb.source_catalog import read_source_revision

    path = merged_table_pdf(tmp_path / "retained.pdf")
    with monkeypatch.context() as older_processor:
        older_processor.setattr("openkb.pdf_layout.PDF_LAYOUT_POLICY", "previous-native-layout")
        # Model a retained package produced by the previous native PDF adapter.
        older_processor.setattr(
            "openkb.images.native_pdf_blocks", lambda page: page.get_text("dict")["blocks"]
        )
        first = import_document(kb_dir, path)
    assert first.status == "added", first.message
    original = read_source_revision(kb_dir, first.source_revision_id).original
    before = read_document_source(kb_dir, first.source_id)
    assert "<table>" not in before["content"]
    retained = {
        p: p.read_bytes() for p in (kb_dir / ".openkb/normalized").rglob("*") if p.is_file()
    }
    path.unlink()

    recompiled = asyncio.run(recompile_document(kb_dir, first.source_id))
    assert recompiled.status == "compiled", recompiled.message
    assert read_document_source(kb_dir, first.source_id)["content"] == before["content"]
    preview = preview_reprocessing(kb_dir, first.source_id)
    assert preview["units"][0]["policy_changed"]
    result = reprocess_source(kb_dir, first.source_id, version=preview["version"])

    assert result.status == "added", result.message
    assert result.source_revision_id != first.source_revision_id
    assert result.units[0].target_revision_id != first.units[0].target_revision_id
    assert read_source_revision(kb_dir, result.source_revision_id).original == original
    assert "<table>" in read_document_source(kb_dir, first.source_id)["content"]
    history = read_document_source(
        kb_dir, first.source_id, source_revision_id=first.source_revision_id
    )
    assert history["content"] == before["content"]
    assert all(p.read_bytes() == content for p, content in retained.items())
