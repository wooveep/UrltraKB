"""An explicit figure caption carries the complete figure evidence through import."""

import io
import json
from zipfile import ZipFile

import pytest
from PIL import Image, ImageDraw

from openkb.application.documents import import_document
from openkb.evidence import BlockDraft
from tests.docx_attachment_fixtures import docx_with_parts


def illustrated_source(path):
    picture = Image.new("RGB", (640, 320), "white")
    ImageDraw.Draw(picture).text((10, 10), "Recovery complete", fill="black")
    data = io.BytesIO()
    picture.save(data, format="PNG")
    return docx_with_parts(
        path,
        "<w:p><w:r><w:t>如下状态为恢复完成。</w:t></w:r></w:p>"
        '<w:p><w:r><w:drawing><wp:inline xmlns:wp="http://schemas.openxmlformats.org/'
        'drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="status"/>'
        "</pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline>"
        "</w:drawing></w:r></w:p>",
        parts={"word/media/status.png": data.getvalue()},
        relationships='<Relationship Id="status" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        'Target="media/status.png"/>',
    )


@pytest.mark.parametrize("image_count", [1, 2])
def test_caption_receives_complete_ocr_and_image_link_at_each_model_stage(
    kb_dir, tmp_path, model_service, monkeypatch, image_count
):
    import openkb.parsing as parsing

    transcription = "Status log. " * 20 + "Number of entries: 0"

    class Ocr:
        def page(self, *args, **kwargs):
            return [BlockDraft(transcription, "paragraph", {"kind": "pdf", "page": 1})], None

        def close(self):
            pass

    monkeypatch.setattr(parsing, "create_ocr", lambda *a, **k: Ocr())
    source = illustrated_source(tmp_path / "recovery.docx")
    if image_count == 2:
        with ZipFile(source) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        document = entries["word/document.xml"].decode()
        start = document.index("<w:p><w:r><w:drawing>")
        end = document.index("</w:p>", start) + len("</w:p>")
        entries["word/document.xml"] = (
            document[:end] + document[start:end] + document[end:]
        ).encode()
        with ZipFile(source, "w") as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    seen = set()
    for request in model_service:
        payload = json.loads(request["messages"][-1]["content"])
        stage = payload.get("stage")
        if stage not in {"facts", "generation", "verification"}:
            continue
        rows = payload["units"] if stage == "facts" else payload["evidence"]
        for row in rows:
            if row["text"] != "如下状态为恢复完成。":
                continue
            neighbors = [
                payload["context_pool"][n["context_ref"]] if "context_ref" in n else n
                for n in row["neighbors"]
            ]
            figures = [n for n in neighbors if n["relation"] == "figure_with_caption"]
            assert len(figures) == image_count
            for figure in figures:
                assert "asset:" in figure["text"] and transcription in figure["text"]
                assert "image_relations" in figure["context_data"]
            seen.add(stage)
    assert seen == {"facts", "generation", "verification"}


@pytest.mark.parametrize("boundary", ["ordinary_text", "new_section"])
def test_adjacency_alone_does_not_bind_a_caption(kb_dir, tmp_path, model_service, boundary):
    source = illustrated_source(tmp_path / "unrelated.docx")
    with ZipFile(source) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    content = entries["word/document.xml"].decode()
    if boundary == "ordinary_text":
        content = content.replace("如下状态为恢复完成。", "另外一个操作已经完成。")
    else:
        content = content.replace(
            "<w:p><w:r><w:drawing>",
            '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
            "<w:r><w:t>Different task</w:t></w:r></w:p><w:p><w:r><w:drawing>",
        )
    entries["word/document.xml"] = content.encode()
    with ZipFile(source, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    for request in model_service:
        payload = json.loads(request["messages"][-1]["content"])
        payload.pop("task_rules", None)  # Rules mention the role; evidence must not claim it.
        assert "figure_with_caption" not in json.dumps(payload)
