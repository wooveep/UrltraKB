"""Real DOC storage objects become independently readable standard documents."""

from io import BytesIO
from pathlib import Path

import pytest
from native_catalog_fixtures import original_bytes

pytest_plugins = ("pending_fixtures", "processing_fixtures")
pytestmark = pytest.mark.usefixtures("prepared_cfb_helper")


@pytest.mark.parametrize(
    "fixture, extension, marker",
    [
        ("embedded-calc.doc", "xls", "EMBEDDED_CALC_STANDARD_MARKER"),
        ("embedded-writer.doc", "doc", "EMBEDDED_WRITER_STANDARD_MARKER"),
    ],
)
def test_doc_substorage_recovers_a_standard_file_then_ordinary_knowledge(
    kb_dir,
    pdf_model,
    office_runtime,
    fixture,
    extension,
    marker,
    live_llm,
    processing_only,
    processing_context,
):
    from native_catalog_fixtures import list_source_units, list_sources
    from processing_fixtures import finish_recovered_sources

    from ultrakb.application.pending import pending_status
    from ultrakb.documents import read_document_source

    calls_before = len(live_llm.calls)
    parent = processing_only(Path(__file__).parent / "fixtures/office" / fixture)
    assert pending_status(kb_dir)["discovery_pending"] == 1
    result = finish_recovered_sources(kb_dir, parent.source_id, processing_context)
    assert result["imports_pending"] == 0, result
    recovered = next(s for s in list_sources(kb_dir) if s.source_id != parent.source_id)
    assert recovered.label.endswith("." + extension)
    source = read_document_source(kb_dir, recovered.source_id)
    assert source["status"] == "completed" and marker in source["content"]
    units = list_source_units(kb_dir, recovered.source_id)
    assert units
    published = "\n".join(
        (kb_dir / f"wiki/summaries/{unit.doc_name}.md").read_text("utf-8") for unit in units
    )
    assert marker in published
    assert len(live_llm.calls) == calls_before


def test_reconstruction_preserves_nested_streams_metadata_and_zero_root_creation(
    kb_dir, pdf_model, processing_only, processing_context
):
    import olefile
    from native_catalog_fixtures import list_sources

    from ultrakb.application.pending import process_pending
    from ultrakb.documents import read_document_source

    parent = processing_only(Path(__file__).parent / "fixtures/office/embedded-metadata.doc")
    process_pending(kb_dir, context=processing_context)
    assert processing_context.executor.calls == 0
    child = next(s for s in list_sources(kb_dir) if s.source_id != parent.source_id)
    result = read_document_source(kb_dir, child.source_id)
    assert "EMBEDDED_CALC_STANDARD_MARKER" in result["content"]
    with olefile.OleFileIO(
        BytesIO(original_bytes(kb_dir, result["source_revision_id"])),
        raise_defects=olefile.DEFECT_INCORRECT,
    ) as restored:
        assert restored.root.createTime == 0 and restored.root.dwUserFlags == 0x12345678
        assert restored.root.modifyTime == 133444736010000000
        nested = restored.direntries[restored._find(["Nested"])]
        assert nested.clsid == restored.root.clsid
        assert nested.createTime == 133444736000000000 and nested.modifyTime == 133444736020000000
        assert restored.exists(["Nested", "Empty"])
        assert restored.openstream(["Nested", "Small"]).read() == b"nested fixture bytes"
        assert restored.openstream(["Nested", "Large"]).read() == b"\xa5" * 9000
        assert restored.direntries[restored._find(["Nested", "Small"])].dwUserFlags == 0x1357


def test_nested_actual_object_is_imported_only_by_its_nearest_document(
    kb_dir, pdf_model, processing_only, processing_context
):
    from native_catalog_fixtures import list_sources

    from ultrakb.application.pending import process_pending
    from ultrakb.documents import read_document_source

    processing_only(Path(__file__).parent / "fixtures/office/embedded-nested.doc")
    result = process_pending(kb_dir, context=processing_context)
    assert processing_context.executor.calls == 0
    children = [s for s in list_sources(kb_dir) if s.label.endswith(".xls")]
    assert len(children) == 1, result
    assert result["groups"][0]["sources"] == 3
    assert (
        "EMBEDDED_CALC_STANDARD_MARKER"
        in read_document_source(kb_dir, children[0].source_id)["content"]
    )
