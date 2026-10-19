"""Native PDF layout changes require explicit new processing revisions."""

import json

import pytest
from native_catalog_fixtures import processing_snapshot

pytest_plugins = ("test_pdf_readback",)


@pytest.mark.parametrize("source_format", ["pdf", "docx", "pptx", "caj"])
@pytest.mark.parametrize(
    "policy, field",
    [
        ("ultrakb.pdf_layout.PDF_LAYOUT_POLICY", "pdf_layout_policy"),
        ("ultrakb.images.PDF_IMAGE_POLICY", "pdf_image_policy"),
        ("ultrakb.source_contract.NATIVE_PAGE_POLICY", "native_page_policy"),
    ],
)
def test_pdf_based_normalization_identity_includes_native_processing_policy(
    kb_dir, monkeypatch, source_format, policy, field
):
    from ultrakb.compile_checkpoints import digest
    from ultrakb.normalization import normalization_policy

    monkeypatch.setattr(
        "ultrakb.office.runtime.processing_identity", lambda *_: {"fixture": "office"}
    )
    monkeypatch.setattr("ultrakb.cnki.runtime.processing_identity", lambda: {"fixture": "cnki"})
    current = normalization_policy(kb_dir, source_format=source_format)
    monkeypatch.setattr(policy, "previous-native-processing")
    previous = normalization_policy(kb_dir, source_format=source_format)

    assert digest(current) != digest(previous)
    assert json.loads(current)[field] != json.loads(previous)[field]


@pytest.mark.parametrize(
    "policy",
    [
        "ultrakb.pdf_layout.PDF_LAYOUT_POLICY",
        "ultrakb.images.PDF_IMAGE_POLICY",
        "ultrakb.source_contract.NATIVE_PAGE_POLICY",
    ],
)
def test_native_text_normalization_does_not_change_with_pdf_policy(kb_dir, monkeypatch, policy):
    from ultrakb.normalization import normalization_policy

    current = normalization_policy(
        kb_dir, source_format="md", input_digest="a" * 64, doc_name="note"
    )
    monkeypatch.setattr(policy, "previous-native-processing")
    assert (
        normalization_policy(kb_dir, source_format="md", input_digest="a" * 64, doc_name="note")
        == current
    )


def test_recompile_retains_old_projection_and_reprocess_creates_immutable_new_version(
    kb_dir, tmp_path, pdf_model, monkeypatch, live_llm
):
    import asyncio

    from native_catalog_fixtures import original_bytes
    from publication_fixtures import publish_document
    from test_pdf_tables import merged_table_pdf

    from ultrakb.application.recompilation import recompile_document
    from ultrakb.application.reprocessing import preview_reprocessing, reprocess_source
    from ultrakb.documents import read_document_source

    path = merged_table_pdf(tmp_path / "retained.pdf")
    with monkeypatch.context() as older_processor:
        older_processor.setattr("ultrakb.pdf_layout.PDF_LAYOUT_POLICY", "previous-native-layout")
        # Model a retained package produced by the previous native PDF adapter.
        older_processor.setattr(
            "ultrakb.images.native_pdf_blocks", lambda page: page.get_text("dict")["blocks"]
        )
        first = publish_document(kb_dir, path, None, bundle=live_llm.credentials)
    assert first.status == "added", first.message
    original = original_bytes(kb_dir, first.source_revision_id)
    before = read_document_source(kb_dir, first.source_id)
    assert "<table>" not in before["content"]
    retained = processing_snapshot(kb_dir)
    path.unlink()

    recompiled = asyncio.run(
        recompile_document(kb_dir, first.source_id, unit_id=first.units[0].unit_id)
    )
    assert recompiled.status == "compiled", recompiled.message
    assert read_document_source(kb_dir, first.source_id)["content"] == before["content"]
    preview = preview_reprocessing(kb_dir, first.source_id)
    assert preview["units"][0]["policy_changed"]
    result = reprocess_source(kb_dir, first.source_id, version=preview["version"])

    assert result.status == "added", result.message
    assert result.source_revision_id != first.source_revision_id
    assert result.units[0].target_revision_id != first.units[0].target_revision_id
    assert original_bytes(kb_dir, result.source_revision_id) == original
    assert "<table>" in read_document_source(kb_dir, first.source_id)["content"]
    history = read_document_source(
        kb_dir, first.source_id, source_revision_id=first.source_revision_id
    )
    assert history["content"] == before["content"]
    assert processing_snapshot(kb_dir, retained) == retained
