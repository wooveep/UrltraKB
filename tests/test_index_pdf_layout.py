"""UltraKB indexing and source reading share the same native PDF structure."""

import shutil
from copy import deepcopy

import pymupdf
import pytest
from pageindex import IndexConfig
from pageindex.backend.input_package import freeze_pages, materialize_pages
from test_index_llm import RecordingIndexLLM
from test_pdf_tables import Tables, merged_table_pdf

from ultrakb.images import convert_pdf_to_pages
from ultrakb.index_client import create_index_client
from ultrakb.index_pdf import NativePdfParser

pytest_plugins = ("test_vendor_sdk",)


class RecordingPdfIndex(RecordingIndexLLM):
    """The shared genuine adapter records structure prompts for layout assertions."""

    @property
    def structure_inputs(self):
        return [messages[-1]["content"] for stage, messages in self.calls if stage == "structure"]


def test_indexed_table_fixture_keeps_existing_geometry_and_body(tmp_path):
    plain = merged_table_pdf(tmp_path / "plain.pdf")
    headed = merged_table_pdf(tmp_path / "headed.pdf", heading="1. Table policies")
    plain_text = convert_pdf_to_pages(plain, "plain", tmp_path / "plain-images")[0]["content"]
    headed_text = convert_pdf_to_pages(headed, "headed", tmp_path / "headed-images")[0]["content"]
    assert headed_text.startswith("1. Table policies\n")
    assert headed_text.partition("\n")[2] == plain_text
    assert Tables(headed_text).rows == Tables(plain_text).rows
    with pymupdf.open(plain) as original, pymupdf.open(headed) as indexed:
        for before, after in zip(
            original[0].get_drawings(), indexed[0].get_drawings(), strict=True
        ):
            assert {key: value for key, value in after.items() if key != "seqno"} == {
                key: value for key, value in before.items() if key != "seqno"
            }
            assert after["seqno"] == before["seqno"] + 1
        for text in ("alpha", "beta", "gamma", "shared-group"):
            assert indexed[0].search_for(text) == original[0].search_for(text)


def test_real_index_input_keeps_the_same_merged_cells_as_source_reading(
    tmp_path, model_service, live_llm
):
    path = merged_table_pdf(tmp_path / "table.pdf", heading="1. Table policies")
    original = path.read_bytes()
    runtime = RecordingPdfIndex(live_llm=live_llm)
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
    assert runtime.structure_inputs
    assert all('rowspan="3"' in prompt for prompt in runtime.structure_inputs)
    assert 'rowspan="3"' in document["structure"][0]["text"]
    assert path.read_bytes() == original

    shutil.copytree(tmp_path / "index", tmp_path / "moved")
    shutil.rmtree(tmp_path / "index")
    path.unlink()
    cached_options = options.model_copy(update={"llm_client": RecordingPdfIndex()})
    with create_index_client(
        storage_path=tmp_path / "moved", index_config=cached_options
    ) as client:
        assert client.collection().get_page_content(identity, "1")[0]["content"] == expected


def test_missing_cache_recovers_with_its_saved_pdf_parser(
    monkeypatch, tmp_path, model_service, live_llm
):
    from pageindex.parser.pdf import PdfParser

    from ultrakb.condb_storage import ConDBPageIndexStorage

    path = merged_table_pdf(tmp_path / "old.pdf", heading="1. Table policies")
    options = IndexConfig(
        llm_client=RecordingPdfIndex(live_llm=live_llm),
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
    sent = len(model_service.requests)
    monkeypatch.setattr(ConDBPageIndexStorage, "get_pages", lambda *args: None)
    cached_options = options.model_copy(update={"llm_client": RecordingPdfIndex()})
    with create_index_client(
        storage_path=tmp_path / "index", index_config=cached_options
    ) as client:
        after = client.collection().get_page_content(identity, "1")[0]["content"]
    assert after == before
    assert len(model_service.requests) == sent


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
