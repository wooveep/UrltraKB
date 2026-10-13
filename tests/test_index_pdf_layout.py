"""OpenKB indexing and source reading share the same native PDF structure."""

import json
import shutil
from copy import deepcopy

import pymupdf
import pytest
from pageindex import IndexConfig
from pageindex.backend.input_package import freeze_pages, materialize_pages
from pageindex.llm import IndexResponse
from test_pdf_tables import Tables, merged_table_pdf

from openkb.images import convert_pdf_to_pages
from openkb.index_client import create_index_client
from openkb.index_pdf import NativePdfParser


class TableIndexModel:
    def __init__(self):
        self.structure_inputs = []

    def complete(self, messages, *, stage):
        prompt = messages[-1]["content"]
        if stage == "structure":
            self.structure_inputs.append(prompt)
            return IndexResponse(
                json.dumps(
                    [
                        {
                            "title": "Before the table: keep this condition.",
                            "structure": "1",
                            "physical_index": "<physical_index_1>",
                        }
                    ]
                )
            )
        if "toc_detected" in prompt:
            return IndexResponse('{"toc_detected":"no"}')
        if "start_begin" in prompt:
            return IndexResponse('{"start_begin":"no"}')
        return IndexResponse('{"answer":"yes"}')

    async def acomplete(self, messages, *, stage):
        return self.complete(messages, stage=stage)


def test_real_index_input_keeps_the_same_merged_cells_as_source_reading(tmp_path):
    path = merged_table_pdf(tmp_path / "table.pdf")
    original = path.read_bytes()
    runtime = TableIndexModel()
    options = IndexConfig(
        llm_client=runtime,
        if_add_node_summary=False,
        if_add_doc_description=False,
        if_add_node_text=True,
    )
    expected = convert_pdf_to_pages(path, "table", tmp_path / "wiki-images")[0]["content"]
    with create_index_client(storage_path=tmp_path / "index", index_config=options) as client:
        collection = client.collection()
        identity = collection.add(str(path))
        document = collection.get_document(identity, include_text=True)
        pages = collection.get_page_content(identity, "1")
    assert pages[0]["content"] == expected
    assert Tables(pages[0]["content"]).rows[1][-1] == {"text": "shared-group", "rowspan": "3"}
    assert all('rowspan="3"' in prompt for prompt in runtime.structure_inputs)
    assert 'rowspan="3"' in document["structure"][0]["text"]
    assert path.read_bytes() == original

    shutil.copytree(tmp_path / "index", tmp_path / "moved")
    shutil.rmtree(tmp_path / "index")
    path.unlink()
    with create_index_client(storage_path=tmp_path / "moved", index_config=options) as client:
        assert client.collection().get_page_content(identity, "1")[0]["content"] == expected


def test_missing_cache_recovers_with_its_saved_pdf_parser(monkeypatch, tmp_path):
    from pageindex.parser.pdf import PdfParser

    from openkb.condb_storage import ConDBPageIndexStorage

    path = merged_table_pdf(tmp_path / "old.pdf")
    options = IndexConfig(
        llm_client=TableIndexModel(),
        if_add_node_summary=False,
        if_add_doc_description=False,
        if_add_node_text=True,
    )
    with create_index_client(storage_path=tmp_path / "index", index_config=options) as client:
        client.register_parser(PdfParser())
        collection = client.collection()
        identity = collection.add(str(path))
        before = collection.get_page_content(identity, "1")[0]["content"]
    assert "<table>" not in before
    monkeypatch.setattr(ConDBPageIndexStorage, "get_pages", lambda *args: None)
    with create_index_client(storage_path=tmp_path / "index", index_config=options) as client:
        after = client.collection().get_page_content(identity, "1")[0]["content"]
    assert after == before


@pytest.mark.parametrize(
    "template", ["Use {} as output file.", "cp {} /tmp/example.png", "![image]({})"]
)
@pytest.mark.parametrize("image_count", [1, 2])
def test_image_relocation_preserves_literal_paths_and_examples(tmp_path, template, image_count):
    from figure_fixtures import png_bytes

    base = tmp_path / "managed"
    image_dir = base / "images"
    literals = [
        template.format(path)
        for path in [
            "sources/images/literals/p1_img1.png",
            "images/p1_img1.png",
            str(image_dir / "p1_img1.png"),
        ]
    ]
    pdf = tmp_path / "literals.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=1200)
        for i, line in enumerate(literals):
            page.insert_text((60, 80 + i * 20), line, fontsize=7)
        for i in range(image_count):
            page.insert_image(
                pymupdf.Rect(60 + i * 150, 180, 160 + i * 150, 280), stream=png_bytes()
            )
        doc.save(pdf)
    parsed = NativePdfParser().parse(str(pdf), images_dir=str(image_dir), doc_name="literals")
    for literal in literals:
        assert literal in parsed.nodes[0].content
    pages = freeze_pages(parsed, base, None)
    for literal in literals:
        assert literal in pages[0]["content"]
    restored = materialize_pages(
        pages, base, {**parsed.metadata, "parser_policy": NativePdfParser.policy}
    )
    for literal in literals:
        assert literal in restored[0]["content"]
    assert len(restored[0]["images"]) == image_count
    changed = deepcopy(pages)
    changed[0]["image_reference_spans"][0][0] += 1
    with pytest.raises(ValueError, match="reference mapping changed"):
        materialize_pages(changed, base, parsed.metadata)
    with pytest.raises(ValueError, match="Unsupported generated image reference policy"):
        materialize_pages(pages, base, {**parsed.metadata, "image_reference_policy": "unknown"})
