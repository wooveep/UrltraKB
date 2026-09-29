"""Markdown input, publication and reading share frozen Unicode coordinates."""

import pytest

pytest_plugins = ("test_pdf_readback",)


def test_markdown_html_and_embedded_images_share_a_safe_asset_namespace(
    kb_dir, tmp_path, pdf_model
):
    import base64

    from openkb.application.documents import import_document
    from openkb.documents import read_document_source

    image = tmp_path / "embedded"
    image.write_bytes(b"local")
    encoded = base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg"/>').decode()
    text = (
        f"![inline](data:image/svg+xml;base64,{encoded})\r\n\r\n"
        '<img src="embedded" width="30">\r\n\r\n'
        '`<img src="code.png">`\r\n'
    )
    source = tmp_path / "assets.md"
    source.write_bytes(text.encode())
    result = import_document(kb_dir, source)
    assert result.status == "added", result.message
    saved = read_document_source(kb_dir, result.source_id)
    assert len(saved["assets"]) == 2 and not saved["diagnostics"]
    assert '<img src="images/assets/embedded_1" width="30">' in saved["content"]
    assert '`<img src="code.png">`' in saved["content"]


def test_markdown_keeps_text_positions_and_frozen_images(kb_dir, tmp_path, pdf_model):
    from openkb.application.documents import import_document
    from openkb.documents import read_document_source
    from openkb.view_records import SourceMetadata

    folder = tmp_path / "inputs"
    folder.mkdir()
    image = folder / "pixel.png"
    image.write_bytes(b"fixed-image-bytes")
    original = (
        "前言 e\u0301。\r\n\r\n# 标题\r\n```py\r\nx = 1\r\n```\r\n\r\n"
        "| A | B |\r\n| - | - |\r\n| 1 | 2 |\r\n![图](pixel.png)\r\n"
    )
    source = folder / "note.md"
    source.write_bytes(original.encode())
    result = import_document(
        kb_dir,
        source,
        metadata=SourceMetadata(product="Text", applicable_versions=("1",), family="manual"),
    )
    assert result.status == "added", result.message
    assert result.source_id is not None
    retained = read_document_source(kb_dir, result.source_id)
    expected = original.replace("(pixel.png)", "(images/note/pixel.png)")
    assert retained["content"] == expected
    assert retained["pages"] is None and retained["tokens"] > 0
    selected = read_document_source(kb_dir, result.source_id, chars="3:5")
    assert selected["content"] == "e\u0301" and selected["char_range"] == [3, 5]
    assert selected["origin_locators"][0]["original_span"] == [3, 5]
    source.unlink()
    image.unlink()
    again = read_document_source(
        kb_dir, result.source_id, source_revision_id=result.source_revision_id
    )
    assert again["content"] == expected
    assert (
        kb_dir / again["base_path"] / "images/note/pixel.png"
    ).read_bytes() == b"fixed-image-bytes"


def test_markdown_reference_images_preserve_code_and_titles(kb_dir, tmp_path, pdf_model):
    from openkb.application.documents import import_document
    from openkb.documents import read_document_source

    (tmp_path / "图 (1).png").write_bytes(b"image")
    original = (
        "`![example](missing.png)`\r\n\r\n```md\r\n![example](missing.png)\r\n```\r\n"
        '\r\n> ![图][ref]\r\n\r\n[ref]: <图 (1).png> "标题"\r\n'
    )
    path = tmp_path / "references.md"
    path.write_bytes(original.encode())
    result = import_document(kb_dir, path)
    assert result.status == "added", result.message
    source = read_document_source(kb_dir, result.source_id)
    assert source["content"].startswith(original.split("> !")[0])
    assert '> ![图](images/references/%E5%9B%BE%20%281%29.png "标题")' in source["content"]
    assert len(source["assets"]) == 1 and not source["diagnostics"]


def test_text_measurement_and_character_ranges_reach_adapters(kb_dir, tmp_path, pdf_model):
    import json

    from click.testing import CliRunner

    from openkb.api_models import DocumentItem, DocumentSourceResponse
    from openkb.application.documents import import_document
    from openkb.application.knowledge_bases import get_kb_list
    from openkb.cli import cli
    from openkb.documents import read_document_source

    path = tmp_path / "hello.md"
    path.write_text("hello world", encoding="utf-8")
    result = import_document(kb_dir, path)
    item = DocumentItem(**get_kb_list(kb_dir)["documents"][0])
    assert item.pages is None and item.tokens == 2 and item.characters == 11
    source = DocumentSourceResponse(**read_document_source(kb_dir, result.source_id))
    assert source.tokens == 2 and source.char_range == [0, 11]
    command = CliRunner().invoke(
        cli, ["--kb-dir", str(kb_dir), "source", result.source_id, "--chars", "6:11"]
    )
    assert command.exit_code == 0, command.output
    assert json.loads(command.output)["content"] == "world"


@pytest.mark.parametrize("tokens,length", [(4999, "short"), (5000, "short"), (5001, "long")])
def test_fixed_markdown_classification_boundary(tokens, length):
    from openkb.processing_policy import classify_markdown_tokens

    decision = classify_markdown_tokens(tokens)
    assert decision.length_class == length and decision.measurement_value == tokens


def test_unavailable_segmentation_keeps_measured_text_readable(kb_dir, tmp_path, pdf_model):
    from openkb.application.documents import import_document
    from openkb.application.knowledge_bases import get_kb_list
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.documents import read_document_source

    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir),
            config={
                "model": "gpt-4o",
                "model_capacity": {"max_input_tokens": 1},
            },
        ),
    )
    path = tmp_path / "hello.md"
    path.write_text("hello world", encoding="utf-8")
    result = import_document(kb_dir, path)
    assert result.status == "failed" and "not available yet" in result.message
    saved = read_document_source(kb_dir, result.source_id, chars="6:11")
    assert saved["knowledge_revision_id"] is None and saved["content"] == "world"
    assert saved["tokens"] == 2 and saved["length_class"] == "short"
    assert get_kb_list(kb_dir)["documents"][0]["characters"] == 11


def test_query_reads_frozen_text_coordinates(kb_dir, tmp_path, pdf_model, monkeypatch):
    import asyncio
    import json

    from litellm import ModelResponse

    from openkb.agent.query import run_query
    from openkb.application.documents import import_document

    path = tmp_path / "hello.md"
    path.write_text("hello world", encoding="utf-8")
    imported = import_document(kb_dir, path)

    async def model(**kwargs):
        outputs = [item["content"] for item in kwargs["messages"] if item["role"] == "tool"]
        message = (
            {"content": outputs[-1]}
            if outputs
            else {
                "tool_calls": [
                    {
                        "id": "read-range",
                        "type": "function",
                        "function": {
                            "name": "get_text_content",
                            "arguments": json.dumps(
                                {
                                    "doc_name": "hello",
                                    "chars": "6:11",
                                    "view_id": imported.units[0].view_id,
                                }
                            ),
                        },
                    }
                ]
            }
        )
        return ModelResponse(
            choices=[
                {
                    "message": {"role": "assistant", **message},
                    "finish_reason": "stop" if outputs else "tool_calls",
                }
            ]
        )

    monkeypatch.setattr("litellm.acompletion", model)
    answer = asyncio.run(run_query("Read the final word of the text", kb_dir, "openai/gpt-4o-mini"))
    assert '"content": "world"' in answer
    assert '"original_span": [6, 11]' in answer and imported.source_revision_id in answer
