"""Markdown input, publication and reading share frozen Unicode coordinates."""

import pytest
from PIL import Image

pytest_plugins = ("test_pdf_readback", "processing_fixtures")


def test_markdown_html_and_embedded_images_share_a_safe_asset_namespace(
    kb_dir, tmp_path, processing_only
):
    import base64

    from ultrakb.documents import read_document_source

    image = tmp_path / "embedded"
    Image.new("RGB", (40, 40), "red").save(image, format="PNG")
    encoded = base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg"/>').decode()
    text = (
        f"![inline](data:image/svg+xml;base64,{encoded})\r\n\r\n"
        '<img src="embedded" width="30">\r\n\r\n'
        '`<img src="code.png">`\r\n'
    )
    source = tmp_path / "assets.md"
    source.write_bytes(text.encode())
    result = processing_only(source)
    saved = read_document_source(kb_dir, result.source_id, unit_revision_id=result.unit_revision_id)
    assert len(saved["assets"]) == 2 and not saved["diagnostics"]
    assert f'<img src="images/{saved["doc_name"]}/embedded_1" width="30">' in saved["content"]
    assert '`<img src="code.png">`' in saved["content"]


def test_markdown_keeps_text_positions_and_frozen_images(kb_dir, tmp_path, processing_only):
    from ultrakb.documents import read_document_source

    folder = tmp_path / "inputs"
    folder.mkdir()
    image = folder / "pixel.png"
    Image.new("RGB", (40, 40), "blue").save(image)
    original_pixels = image.read_bytes()
    original = (
        "前言 e\u0301。\r\n\r\n# 标题\r\n```py\r\nx = 1\r\n```\r\n\r\n"
        "| A | B |\r\n| - | - |\r\n| 1 | 2 |\r\n![图](pixel.png)\r\n"
    )
    source = folder / "note.md"
    source.write_bytes(original.encode())
    result = processing_only(source)
    assert result.source_id is not None
    retained = read_document_source(
        kb_dir, result.source_id, unit_revision_id=result.unit_revision_id
    )
    expected = original.replace("(pixel.png)", f"(images/{retained['doc_name']}/pixel.png)")
    assert retained["content"] == expected
    assert retained["pages"] is None and retained["tokens"] > 0
    selected = read_document_source(
        kb_dir, result.source_id, unit_revision_id=result.unit_revision_id, chars="3:5"
    )
    assert selected["content"] == "e\u0301" and selected["char_range"] == [3, 5]
    assert selected["origin_locators"][0]["original_span"] == [3, 5]
    source.unlink()
    image.unlink()
    again = read_document_source(
        kb_dir,
        result.source_id,
        source_revision_id=result.source_revision_id,
        unit_revision_id=result.unit_revision_id,
    )
    assert again["content"] == expected
    from ultrakb.application.storage_sources import read_source_image
    from ultrakb.application.views import published_scope

    assert (
        read_source_image(
            kb_dir,
            result.source_id,
            result.unit_revision_id,
            f"sources/images/{retained['doc_name']}/pixel.png",
            identity=published_scope(kb_dir).knowledge.identity,
        )[0]
        == original_pixels
    )


def test_markdown_reference_images_preserve_code_and_titles(kb_dir, tmp_path, processing_only):
    from ultrakb.documents import read_document_source

    Image.new("RGB", (40, 40), "green").save(tmp_path / "图 (1).png")
    original = (
        "`![example](missing.png)`\r\n\r\n```md\r\n![example](missing.png)\r\n```\r\n"
        '\r\n> ![图][ref]\r\n\r\n[ref]: <图 (1).png> "标题"\r\n'
    )
    path = tmp_path / "references.md"
    path.write_bytes(original.encode())
    result = processing_only(path)
    source = read_document_source(
        kb_dir, result.source_id, unit_revision_id=result.unit_revision_id
    )
    assert source["content"].startswith(original.split("> !")[0])
    assert (
        f'> ![图](images/{source["doc_name"]}/%E5%9B%BE%20%281%29.png "标题")' in source["content"]
    )
    assert len(source["assets"]) == 1 and not source["diagnostics"]


def test_text_measurement_and_character_ranges_reach_adapters(kb_dir, tmp_path, processing_only):
    import json

    from click.testing import CliRunner

    from ultrakb.api_models import DocumentItem, DocumentSourceResponse
    from ultrakb.application.knowledge_bases import get_kb_list
    from ultrakb.cli import cli
    from ultrakb.documents import read_document_source

    path = tmp_path / "hello.md"
    path.write_text("hello world", encoding="utf-8")
    result = processing_only(path)
    item = DocumentItem(**get_kb_list(kb_dir)["documents"][0])
    assert item.pages is None and item.tokens == 2 and item.characters == 11
    source = DocumentSourceResponse(
        **read_document_source(kb_dir, result.source_id, unit_revision_id=result.unit_revision_id)
    )
    assert source.tokens == 2 and source.char_range == [0, 11]
    command = CliRunner().invoke(
        cli,
        [
            "--kb-dir",
            str(kb_dir),
            "source",
            result.source_id,
            "--chars",
            "6:11",
            "--unit-revision",
            result.unit_revision_id,
        ],
    )
    assert command.exit_code == 0, command.output
    assert json.loads(command.output)["content"] == "world"


@pytest.mark.parametrize("tokens,length", [(4999, "short"), (5000, "short"), (5001, "long")])
def test_fixed_markdown_classification_boundary(tokens, length):
    from ultrakb.processing_policy import classify_markdown_tokens

    decision = classify_markdown_tokens(tokens)
    assert decision.length_class == length and decision.measurement_value == tokens


def test_unadmitted_generation_keeps_measured_text_readable(kb_dir, tmp_path, processing_only):
    from ultrakb.application.knowledge_bases import get_kb_list
    from ultrakb.documents import read_document_source

    path = tmp_path / "hello.md"
    path.write_text("hello world", encoding="utf-8")
    result = processing_only(path)
    saved = read_document_source(
        kb_dir, result.source_id, unit_revision_id=result.unit_revision_id, chars="6:11"
    )
    assert saved["knowledge_revision_id"] is None and saved["content"] == "world"
    assert saved["tokens"] == 2 and saved["length_class"] == "short"
    assert get_kb_list(kb_dir)["documents"][0]["characters"] == 11


def test_query_reads_frozen_text_coordinates(kb_dir, tmp_path, pdf_model, live_llm):
    import asyncio
    import json

    from agents import RunConfig

    from ultrakb.agent.query import run_query
    from ultrakb.application.documents import import_document

    path = tmp_path / "hello.md"
    path.write_text("hello world", encoding="utf-8")
    imported = import_document(kb_dir, path)
    before = len(live_llm.requests)
    answer = asyncio.run(
        run_query(
            "Use get_text_content to read hello with chars='6:11'. Quote only the returned word.",
            kb_dir,
            "deepseek/deepseek-flash",
            bundle=live_llm.credentials,
            run_config=RunConfig(tracing_disabled=True),
        )
    )
    assert "world" in answer.lower()
    delivered = [
        message["content"]
        for request in live_llm.requests[before:]
        for message in request["messages"]
        if message["role"] == "tool"
    ]
    assert any('"content": "world"' in text for text in delivered)
    assert any('"original_span": [6, 11]' in text for text in delivered)
    assert imported.source_revision_id in json.dumps(delivered)
