"""OCR is supplementary; retained figures reach source context and answer rendering."""

import io
import json

import pytest
from PIL import Image, ImageDraw

from openkb.application.documents import import_document
from openkb.evidence import ParseStore
from tests.docx_attachment_fixtures import docx_with_parts


def _illustrated_docx(path):
    image = Image.new("RGB", (320, 180), "white")
    ImageDraw.Draw(image).text((10, 10), "Restart control 9473", fill="black")
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    docx_with_parts(
        path,
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Recovery</w:t></w:r></w:p>'
        "<w:p><w:r><w:t>Restart the control service on port 9473. The figure shows the panel.</w:t>"
        '<w:drawing><wp:inline xmlns:wp="http://schemas.openxmlformats.org/'
        'drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="panel"/>'
        "</pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing>"
        "</w:r></w:p>",
        parts={"word/media/panel.png": stream.getvalue()},
        relationships='<Relationship Id="panel" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        'Target="media/panel.png"/>',
    )
    return path


def test_ocr_exception_does_not_block_compilation_and_figure_keeps_text_context(
    kb_dir, tmp_path, monkeypatch, model_service
):
    import openkb.parsing as parsing
    from openkb.agent.tools import read_wiki_file, read_wiki_image

    class BrokenOcr:
        def page(self, *args, **kwargs):
            raise RuntimeError("Private provider credentials must not enter diagnostics")

        def close(self):
            pass

    monkeypatch.setattr(parsing, "create_ocr", lambda *a, **k: BrokenOcr())
    result = import_document(kb_dir, _illustrated_docx(tmp_path / "manual.docx"))
    assert result.knowledge_compilation == "completed", result
    assert any("ocr_backend_failed:RuntimeError" in x for x in result.warnings)
    assert "Private provider" not in str(result)
    parsed = ParseStore(kb_dir).load(result.parse_id)
    block = next(b for b in parsed.blocks if b.assets)
    assert block.location["headings"] == ["Recovery"]
    assert block.location["paragraph"] == 2
    source = next((kb_dir / "wiki/sources").glob("*.md"))
    text = read_wiki_file(source.relative_to(kb_dir / "wiki").as_posix(), str(kb_dir / "wiki"))
    catalog = json.loads(text.split("contents):\n")[-1])
    assert "port 9473" in catalog[0]["adjacent_text"]
    assert catalog[0]["path"].startswith("sources/images/")
    assert read_wiki_image(catalog[0]["path"], str(kb_dir / "wiki"))["type"] == "image"


def test_catalog_handles_note_relative_paths_without_exposing_outside_files(kb_dir):
    from openkb.agent.tools import read_wiki_file

    root = kb_dir / "wiki"
    Image.new("RGB", (20, 20), "blue").save(root / "sources/images/panel.png")
    (root / "concepts/recovery.md").write_text(
        "Restart port 9473.\n\n![Control panel](../sources/images/panel.png)\n\n"
        "`![example](fake.png)`\n\n![outside](../../outside.png)\n\n"
        "![malformed](images/%00.png)\n\n![malformed-url](http://[invalid]/image.png)"
    )
    text = read_wiki_file("concepts/recovery.md", str(root))
    catalog = json.loads(text.split("contents):\n")[-1])
    assert len(catalog) == 1
    assert catalog[0]["path"] == "sources/images/panel.png"
    assert "Restart port 9473" in catalog[0]["adjacent_text"]


def test_table_header_figure_resolves_in_later_cell_context(kb_dir, tmp_path):
    from openkb.application.document_pipeline import _materialize
    from openkb.evidence import BlockDraft
    from openkb.inputs import prepared_input
    from openkb.sources import SourceStore

    original = tmp_path / "table.txt"
    original.write_text("Table source with a shared header figure.")
    store = SourceStore(kb_dir)
    with prepared_input(original) as ready:
        source = store.intake(ready)
    data = io.BytesIO()
    Image.new("RGB", (320, 180), "blue").save(data, format="PNG")
    digest = store.put_bytes(data.getvalue())
    figure = f"![Control panel](asset:{digest})"
    parsed = ParseStore(kb_dir).save(
        source,
        {"test": "shared-header-figure"},
        [
            BlockDraft(figure, "table", {"kind": "docx", "paragraph": 1}, (digest,)),
            BlockDraft(
                "Restart port 9473.",
                "table",
                {"kind": "docx", "paragraph": 2},
                context="Table header: " + figure,
            ),
        ],
    )
    path = _materialize(kb_dir, store, source, parsed, "table")
    text = path.read_text("utf-8")
    assert "asset:" not in text
    assert text.count(f"images/{digest}.png") == 2
    assert "Table header:" in text and "Restart port 9473" in text


def test_optional_ocr_does_not_swallow_cancellation(kb_dir, tmp_path):
    from openkb.cancellation import OperationCancelled
    from openkb.ocr.optional import recognize

    class StoppedOcr:
        def page(self, *args, **kwargs):
            raise OperationCancelled()

    with pytest.raises(OperationCancelled):
        recognize(StoppedOcr(), None, 1)


def test_windows_ocr_probe_timeout_is_optional_but_document_deadline_is_not(monkeypatch):
    from types import SimpleNamespace

    import openkb.ocr.backend as backend
    import openkb.ocr.windows as windows
    from openkb.processing import ProcessingIncomplete

    monkeypatch.setattr(backend, "os", SimpleNamespace(name="nt"))
    settings = SimpleNamespace(backend="local", local=None)
    reason = "windows_ocr_timeout"

    def unavailable():
        raise ProcessingIncomplete(reason, "ocr")

    monkeypatch.setattr(windows, "runtime_profile", unavailable)
    assert backend.default_local_profile(settings) is None
    reason = "document_timeout"
    with pytest.raises(ProcessingIncomplete):
        backend.default_local_profile(settings)


def test_reparse_task_completes_without_claiming_knowledge_compilation(kb_dir, tmp_path):
    from openkb.inputs import prepared_input
    from openkb.runtime.requests import ReparseSource
    from openkb.runtime.tasks import TaskManager
    from openkb.sources import SourceStore

    path = tmp_path / "notes.txt"
    path.write_text("Text-only source for parsing.")
    with prepared_input(path) as ready:
        source = SourceStore(kb_dir).intake(ready)
    manager = TaskManager(history_dir=tmp_path / "history")
    try:
        task = manager.submit(kb_dir, [ReparseSource(source.source_id, source.id)])
        result = manager.wait(task, timeout=20)
        assert result.state == "completed" and result.succeeded == 1
        assert result.results[0].document.knowledge_compilation == "not_started"
        assert result.processes_reaped
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)


def test_unrenderable_original_image_is_downloadable_without_blocking_text(
    kb_dir, tmp_path, model_service
):
    from openkb.agent.tools import read_wiki_file

    source = tmp_path / "legacy.md"
    source.write_text("Restart control on port 9473.\n\n![Legacy panel](legacy.png)")
    original = b"Unsupported original drawing bytes"
    (tmp_path / "legacy.png").write_bytes(original)
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert any("source_image_preview_unavailable:" in item for item in result.warnings)
    saved = next((kb_dir / "wiki/sources/attachments").glob("*.bin"))
    assert saved.read_bytes() == original
    page = next((kb_dir / "wiki/sources").glob("*.md"))
    text = read_wiki_file(page.relative_to(kb_dir / "wiki").as_posix(), str(kb_dir / "wiki"))
    assert "9473" in text and ".bin)" in text
    assert "![Legacy panel]" not in text
