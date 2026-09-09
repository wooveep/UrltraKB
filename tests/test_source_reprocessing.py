"""Source-only reprocessing preserves committed knowledge and historical evidence."""

from openkb.application.documents import import_document
from openkb.application.source_actions import read_source_evidence
from openkb.evidence import Evidence, ParseStore


def test_reparse_keeps_old_evidence_and_does_not_regenerate_knowledge(
    kb_dir, tmp_path, model_service
):
    from openkb.application.source_actions import reparse_source

    source = tmp_path / "notes.md"
    source.write_text("Source fact: timeout is 42 seconds.")
    imported = import_document(kb_dir, source)
    parsed = ParseStore(kb_dir).load(imported.parse_id)
    old = Evidence(imported.source_id, imported.input_version, parsed.id, parsed.blocks[0].id)
    before = {
        p.relative_to(kb_dir): p.read_bytes() for p in (kb_dir / "wiki").rglob("*") if p.is_file()
    }
    calls = len(model_service)
    source.unlink()
    result = reparse_source(kb_dir, imported.source_id, version_id=imported.input_version)
    assert result.source_intake == "saved" and result.stage == "parsed"
    assert result.knowledge_compilation == "not_started"
    assert len(model_service) == calls
    assert {
        p.relative_to(kb_dir): p.read_bytes() for p in (kb_dir / "wiki").rglob("*") if p.is_file()
    } == before
    assert (
        read_source_evidence(kb_dir, old, max_chars=100).text
        == "Source fact: timeout is 42 seconds."
    )


def test_review_renders_the_selected_physical_page_from_the_retained_version(kb_dir, tmp_path):
    import io

    import pymupdf
    from PIL import Image

    from openkb.application.source_actions import source_page_image

    source = tmp_path / "pages.pdf"
    with pymupdf.open() as pdf:
        for color in ((1, 0, 0), (0, 0, 1)):
            page = pdf.new_page(width=200, height=100)
            page.draw_rect(page.rect, color=color, fill=color)
        pdf.save(source)
    result = import_document(kb_dir, source)
    source.unlink()
    original = source_page_image(
        kb_dir, result.source_id, version_id=result.input_version, page=2, width=100, height=100
    )
    with Image.open(io.BytesIO(original)) as image:
        assert image.size == (100, 50)
        assert image.getpixel((50, 25)) == (0, 0, 255)
