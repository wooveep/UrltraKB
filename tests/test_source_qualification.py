"""Source roles agree at freezing, indexing and original evidence interfaces."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from pageindex.index.block_policy import BlockPolicy
from test_answer_evidence_regression import agent_for, invoke

from openkb.agent.evidence_payload import project_evidence
from openkb.block_package import FrozenBlockParser, freeze_block_package
from openkb.locks import atomic_write_text
from openkb.text_formats import normalize_text_document
from openkb.text_source import read_text_selection

pytest_plugins = ("test_vendor_sdk",)


def freeze_source(kb_dir, tmp_path, extension, text):
    path = tmp_path / f"input.{extension}"
    path.write_text(text)
    prepared = SimpleNamespace(
        path=path, source=path, images={}, digest=hashlib.sha256(path.read_bytes()).hexdigest()
    )
    frozen = normalize_text_document(prepared, "manual", kb_dir / "wiki")
    source = kb_dir / "wiki/sources/manual.md"
    atomic_write_text(source, frozen.text)
    atomic_write_text(source.with_suffix(".content.json"), frozen.model_dump_json())
    return source, frozen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extension,text,titles,contexts",
    [
        ("txt", "# deployment comment\n\nOUTSIDE.\n", [], []),
        ("md", "# Root\n\n> ## Quoted example\n> quoted text\n\nOUTSIDE.\n", ["Root"], ["# Root"]),
        (
            "md",
            "# Root\n\n<div>\n\n## Contained example\n\n</div>\n\nOUTSIDE.\n",
            ["Root"],
            ["# Root"],
        ),
        (
            "md",
            "Root\r\n====\r\n\r\n## Child\r\n\r\nOUTSIDE.\r\n",
            ["Root", "Child"],
            ["Root\r\n====", "## Child"],
        ),
        (
            "html",
            "<h1>Root</h1><blockquote><h2>Quoted</h2></blockquote><p>OUTSIDE.</p>",
            ["Root"],
            ["# Root"],
        ),
    ],
)
async def test_heading_roles_agree_in_new_index_and_public_continuation(
    kb_dir, tmp_path, extension, text, titles, contexts
):
    source, frozen = freeze_source(kb_dir, tmp_path, extension, text)
    package = freeze_block_package(source, "manual")
    parsed = FrozenBlockParser().parse(str(package), images_dir=str(tmp_path / "index/images"))
    assert [h["title"] for node in parsed.nodes for h in node.metadata["headings"]] == titles
    policy = BlockPolicy(parsed.metadata)
    for node in parsed.nodes:
        assert [h["title"] for h in json.loads(policy.render(node.index))["headings"]] == titles
    raw = json.loads(source.with_suffix(".content.json").read_text())
    offset = frozen.text.index("OUTSIDE")
    projected = project_evidence(read_text_selection(raw), offset=offset)
    assert [item["text"].strip() for item in projected.get("display_context", [])] == contexts
    from openkb.agent.source_windows import CompileSourceWindows

    compiled = CompileSourceWindows(kb_dir / "wiki", "manual").read_selection(
        "sources/manual.content.json", {"blocks": "1"}
    )[0]
    assert [
        h["title"] for block in compiled.get("blocks", []) for h in block.get("headings", [])
    ] == titles
    source.unlink()  # Publish the frozen mapping alone, as in the public-reader fixtures.
    response = await invoke(
        agent_for(kb_dir), "get_block_content", doc_name="manual", blocks="1", offset=offset
    )
    public = json.loads(response.split("\n\n", 1)[1])
    assert "content" in public, public
    assert public["content"] == frozen.text[offset:]
    assert [
        h["title"] for block in public.get("blocks", []) for h in block.get("headings", [])
    ] == titles
    assert [item["text"].strip() for item in public.get("display_context", [])] == contexts
    assert [h["text"] for h in public["source_context"]["regions"][0]["headings"]] == titles


@pytest.mark.parametrize(
    "extension,title,expected", [("txt", "comment", "generated"), ("md", "Root", "original")]
)
def test_real_block_index_cannot_upgrade_a_literal_comment(
    kb_dir, tmp_path, extension, title, expected
):
    from pageindex import IndexConfig
    from pageindex.llm import IndexResponse

    from openkb.index_client import create_index_client

    text = f"# {title}\n\nThe body must remain available.\n"
    source, frozen = freeze_source(kb_dir, tmp_path, extension, text)
    package = freeze_block_package(source, "manual")
    entry = {
        "structure": "1",
        "title": title,
        "title_origin": "original",
        "physical_index": 1,
        "anchor": {
            "unit": 1,
            "part": "body",
            "range": [0, len(f"# {title}\n")],
            "excerpt": f"# {title}\n",
        },
    }

    class Model:
        def complete(self, messages, *, stage):
            return IndexResponse(json.dumps([entry]))

        async def acomplete(self, messages, *, stage):
            return self.complete(messages, stage=stage)

    options = IndexConfig(
        llm_client=Model(),
        if_add_node_summary=False,
        if_add_doc_description=False,
        if_add_node_text=True,
    )
    with create_index_client(storage_path=tmp_path / "index", index_config=options) as client:
        collection = client.collection()
        identity = collection.add(str(package))
        result = collection.get_document(identity, include_text=True)
        assert result["structure"][0]["title_origin"] == expected
        assert result["structure"][0]["text"] == frozen.text


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["script", "style", "title", "template"])
async def test_hidden_table_text_is_not_a_cell_value_in_compile_or_question(kb_dir, tmp_path, tag):
    from openkb.agent.source_windows import CompileSourceWindows

    text = (
        f"<table><tr><td>1<{tag}>999</{tag}></td><td>visible &amp; text<br>tail</td></tr></table>\n"
    )
    source, frozen = freeze_source(kb_dir, tmp_path, "md", text)
    raw = json.loads(source.with_suffix(".content.json").read_text())
    projected = project_evidence(read_text_selection(raw))
    compiled = CompileSourceWindows(kb_dir / "wiki", "manual").read_selection(
        "sources/manual.content.json", {"chars": f"0:{len(frozen.text)}"}
    )[0]
    source.unlink()
    response = await invoke(
        agent_for(kb_dir), "get_text_content", doc_name="manual", chars=f"0:{len(frozen.text)}"
    )
    public = json.loads(response.split("\n\n", 1)[1])
    for payload in [projected, compiled, public]:
        assert payload["content"] == text
        table = payload["native_tables"][0]
        assert table["status"] == "observed"
        assert [cell["value"] for cell in table["cells"]] == ["1", "visible & text\ntail"]
        assert all(cell["value_coverage"] == "complete" for cell in table["cells"])


@pytest.mark.parametrize("role", ["document_index", "compile", "answer"])
def test_native_pdf_image_syntax_remains_text_at_the_model_boundary(tmp_path, model_service, role):
    import pymupdf
    from test_llm_execution import executor_for
    from test_vendor_sdk import response

    from openkb.index_pdf import NativePdfParser
    from openkb.llm_execution import ModelRequest
    from openkb.source_pages import read_page_selection

    literal = "![image](data:image/png;base64,YWJj)"
    path = tmp_path / "literal.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((60, 60), "A literal Markdown example:")
        page.insert_text((60, 90), literal)
        document.save(path)
    parsed = NativePdfParser().parse(str(path), images_dir=str(tmp_path / "images"))
    assert not parsed.nodes[0].images and literal in parsed.nodes[0].content
    selected = read_page_selection(
        [{"page": 1, "unit_kind": "page", "content": parsed.nodes[0].content, "images": []}], "1"
    )
    projected = project_evidence(selected)
    model_service.replies.append((200, response()))
    request = ModelRequest(
        [{"role": "user", "content": json.dumps(projected)}], operation="source", stage="original"
    )
    executor_for(model_service).complete(role, request)
    assert len(model_service.requests) == 1
    assert literal in model_service.requests[0]["messages"][0]["content"]
    assert "figure_interpretation" not in projected


def test_legacy_block_cache_keeps_its_bytes_but_does_not_certify_old_headings(
    kb_dir, tmp_path, monkeypatch
):
    from pageindex import IndexConfig
    from pageindex.llm import IndexResponse

    from openkb.block_package import BlockPackage
    from openkb.condb_storage import ConDBPageIndexStorage
    from openkb.content_blocks import LEGACY_BLOCK_POLICY, ContentBlock, SourceHeading
    from openkb.index_client import create_index_client

    text = "# old comment\n\nStored original body.\n"
    source, frozen = freeze_source(kb_dir, tmp_path, "txt", text)
    legacy = frozen.model_copy(
        update={
            "unit_kind": "block",
            "block_policy": LEGACY_BLOCK_POLICY,
            "blocks": (
                ContentBlock(
                    ordinal=1,
                    source_spans=((0, len(text)),),
                    headings=(SourceHeading(title="old comment", source_span=(0, 14)),),
                ),
            ),
        }
    )
    package = source.with_suffix(".okbi")
    atomic_write_text(
        package, BlockPackage(doc_name="manual", source=legacy, assets={}).model_dump_json()
    )
    bytes_before = package.read_bytes()
    parsed = FrozenBlockParser(legacy=True).parse(
        str(package), images_dir=str(tmp_path / "old/images")
    )
    assert parsed.nodes[0].metadata["headings"][0]["title"] == "old comment"
    from openkb.block_package import read_block_selection

    projection = project_evidence(read_block_selection(legacy.model_dump(mode="json")), offset=16)
    assert all(not block.get("headings") for block in projection.get("blocks", []))
    assert not projection.get("display_context")

    class Model:
        def complete(self, messages, *, stage):
            return IndexResponse(
                json.dumps(
                    [
                        {
                            "title": "old comment",
                            "title_origin": "original",
                            "structure": "1",
                            "physical_index": 1,
                            "anchor": {
                                "unit": 1,
                                "part": "body",
                                "range": [0, 14],
                                "excerpt": text[:14],
                            },
                        }
                    ]
                )
            )

        async def acomplete(self, messages, *, stage):
            return self.complete(messages, stage=stage)

    options = IndexConfig(
        llm_client=Model(), if_add_node_summary=False, if_add_doc_description=False
    )
    with create_index_client(storage_path=tmp_path / "old", index_config=options) as client:
        client.register_parser(FrozenBlockParser(legacy=True))
        identity = client.collection().add(str(package))
        before = client.collection().get_block_content(identity, "1")
    monkeypatch.setattr(ConDBPageIndexStorage, "get_pages", lambda *args: None)
    with create_index_client(storage_path=tmp_path / "old", index_config=options) as client:
        after = client.collection().get_block_content(identity, "1")
    assert before == after
    assert package.read_bytes() == bytes_before


@pytest.mark.parametrize("role", ["headings", "heading_path", "table_header"])
def test_new_block_parser_rejects_forged_source_structure(kb_dir, tmp_path, role):
    from openkb.block_package import BlockPackage
    from openkb.content_blocks import BLOCK_POLICY, ContentBlock, DisplayContext, SourceHeading

    source, frozen = freeze_source(kb_dir, tmp_path, "txt", "# comment\n")
    forged = frozen.model_copy(
        update={
            "unit_kind": "block",
            "block_policy": BLOCK_POLICY,
            "blocks": (
                ContentBlock(
                    ordinal=1,
                    source_spans=((0, 10),),
                    headings=(SourceHeading(title="comment", source_span=(0, 10)),)
                    if role == "headings"
                    else (),
                    display_context=(
                        DisplayContext(kind=role, text=frozen.text, source_spans=((0, 10),)),
                    )
                    if role != "headings"
                    else (),
                ),
            ),
        }
    )
    package = source.with_suffix(".okbi")
    atomic_write_text(
        package, BlockPackage(doc_name="manual", source=forged, assets={}).model_dump_json()
    )
    with pytest.raises(ValueError, match="unqualified original"):
        FrozenBlockParser().parse(str(package), images_dir=str(tmp_path / "index/images"))


@pytest.mark.parametrize("container", ["quote", "fence", "html"])
def test_reading_a_fragment_does_not_lose_the_original_literal_context(kb_dir, tmp_path, container):
    texts = {
        "quote": "# Root\n\n> ## Example\n> body\n\nOUTSIDE.\n",
        "fence": "# Root\n\n```text\n## Example\nbody\n```\n\nOUTSIDE.\n",
        "html": "# Root\n\n<div>\n\n## Example\nbody\n</div>\n\nOUTSIDE.\n",
    }
    source, frozen = freeze_source(kb_dir, tmp_path, "md", texts[container])
    raw = json.loads(source.with_suffix(".content.json").read_text())
    left, right = frozen.text.index("## Example"), len(frozen.text)
    selected = read_text_selection(raw, f"{left}:{right}")
    projected = project_evidence(selected, offset=selected["content"].index("OUTSIDE"))
    assert not projected.get("display_context")
    assert projected["content"] == "OUTSIDE.\n"


@pytest.mark.parametrize("tag", ["code", "pre", "xmp"])
def test_visible_literal_cell_text_is_preserved(kb_dir, tmp_path, tag):
    text = f"<table><tr><td>1<{tag}>999</{tag}></td></tr></table>\n"
    source, _ = freeze_source(kb_dir, tmp_path, "md", text)
    projected = project_evidence(
        read_text_selection(json.loads(source.with_suffix(".content.json").read_text()))
    )
    assert projected["native_tables"][0]["cells"][0]["value"] == "1999"
    assert projected["content"] == text


@pytest.mark.parametrize("tag", ["iframe", "textarea", "noscript"])
def test_unresolved_html_controls_never_certify_a_plain_cell_value(kb_dir, tmp_path, tag):
    text = f"<table><tr><td>1<{tag}></{tag}></td></tr></table>\n"
    source, _ = freeze_source(kb_dir, tmp_path, "md", text)
    projected = project_evidence(
        read_text_selection(json.loads(source.with_suffix(".content.json").read_text()))
    )
    cell = projected["native_tables"][0]["cells"][0]
    assert cell["kind"] == "unresolved" and "value" not in cell
    assert projected["content"] == text


@pytest.mark.asyncio
@pytest.mark.parametrize("extension,fenced", [("md", False), ("md", True), ("txt", False)])
async def test_table_qualification_survives_character_selection(
    kb_dir, tmp_path, extension, fenced
):
    from openkb.agent.source_windows import CompileSourceWindows

    table = "<table><tr><td>1</td><td>visible</td></tr></table>"
    text = f"```html\n{table}\n```\n" if fenced else table + "\n"
    source, frozen = freeze_source(kb_dir, tmp_path, extension, text)
    raw = json.loads(source.with_suffix(".content.json").read_text())
    start = frozen.text.index(table)
    chars = f"{start}:{start + len(table)}"
    projected = project_evidence(read_text_selection(raw, chars))
    compiled = CompileSourceWindows(kb_dir / "wiki", "manual").read_selection(
        "sources/manual.content.json", {"chars": chars}
    )[0]
    source.unlink()
    public = json.loads(
        (await invoke(agent_for(kb_dir), "get_text_content", doc_name="manual", chars=chars)).split(
            "\n\n", 1
        )[1]
    )
    for payload in (projected, compiled, public):
        assert payload["content"] == table
        if extension == "md" and not fenced:
            assert [cell["value"] for cell in payload["native_tables"][0]["cells"]] == [
                "1",
                "visible",
            ]
        else:
            assert not payload.get("native_tables")


def test_partial_table_selection_keeps_merged_identity_without_unread_values(kb_dir, tmp_path):
    text = (
        '<table><tr><td rowspan="2">unread group</td><td>first row</td></tr>'
        "<tr><td>second row</td></tr></table>\n"
    )
    source, frozen = freeze_source(kb_dir, tmp_path, "md", text)
    raw = json.loads(source.with_suffix(".content.json").read_text())
    start, end = frozen.text.index("<tr><td>second"), frozen.text.index("</table>")
    selected = read_text_selection(raw, f"{start}:{end}")
    assert "unread group" not in json.dumps(selected, ensure_ascii=False)
    projected = project_evidence(selected)
    assert projected["content"] == text[start:end]
    table = projected["native_tables"][0]
    assert table["coverage"] == "partial" and table["status"] == "observed"
    assert table["rows"] == [{"row": 2, "cells": ["r1c1", "r2c2"]}]
    cells = {cell["id"]: cell for cell in table["cells"]}
    assert cells["r1c1"]["value_coverage"] == "unread" and "value" not in cells["r1c1"]
    assert cells["r1c1"]["rows"] == [1, 2]
    assert cells["r2c2"]["value_coverage"] == "complete"
    assert cells["r2c2"]["value"] == "second row"


def test_nested_hidden_template_keeps_cell_value_and_following_table(kb_dir, tmp_path):
    text = (
        "<table><tr><td>1<template><template>999<br></template>"
        "<table><tr><td>888</td></tr></table></template>2</td></tr></table>\n\n"
        "<table><tr><td>3</td></tr></table>\n"
    )
    source, _ = freeze_source(kb_dir, tmp_path, "md", text)
    projected = project_evidence(
        read_text_selection(json.loads(source.with_suffix(".content.json").read_text()))
    )
    assert [table["cells"][0]["value"] for table in projected["native_tables"]] == ["12", "3"]
    assert projected["content"] == text


@pytest.mark.parametrize(
    "extension,container", [("md", ""), ("md", "fence"), ("md", "html"), ("txt", "")]
)
def test_table_header_continuation_keeps_full_source_role(kb_dir, tmp_path, extension, container):
    table = "Name | Value\n--- | ---\nfirst | one\nsecond | two\n"
    texts = {"": table, "fence": f"```text\n{table}```\n", "html": f"<div>\n\n{table}\n</div>\n"}
    source, frozen = freeze_source(kb_dir, tmp_path, extension, texts[container])
    raw = json.loads(source.with_suffix(".content.json").read_text())
    start = frozen.text.index(table)
    selected = read_text_selection(raw, f"{start}:{start + len(table)}")
    projected = project_evidence(selected, offset=selected["content"].index("second"))
    headers = [
        item for item in projected.get("display_context", []) if item["kind"] == "table_header"
    ]
    assert bool(headers) == (extension == "md" and not container)
    assert projected["content"] == "second | two\n"


@pytest.mark.asyncio
async def test_sparse_blocks_cannot_extend_table_header_scope(kb_dir, tmp_path):
    from openkb.agent.source_windows import CompileSourceWindows
    from openkb.block_package import read_block_selection

    table = "| Label | Value |\n| --- | --- |\n" + "| row | retained table value |\n" * 320
    text = table + "\n\nOUTSIDE is ordinary prose.\n" + "z" * 2400 + "\n"
    source, _ = freeze_source(kb_dir, tmp_path, "md", text)
    freeze_block_package(source, "manual")
    raw = json.loads(source.with_suffix(".content.json").read_text())
    outside = text.index("OUTSIDE")
    ordinal = next(
        block["ordinal"]
        for block in raw["blocks"]
        if any(a <= outside < b for a, b in block["source_spans"])
    )
    blocks = f"1,{ordinal}"
    selected = read_block_selection(raw, blocks)
    offset = selected["content"].index("OUTSIDE")
    projected = project_evidence(selected, offset=offset)
    compiled = list(
        CompileSourceWindows(kb_dir / "wiki", "manual").iter_selection(
            "sources/manual.content.json", {"blocks": blocks, "offset": offset}, allow_suffix=True
        )
    )[0]
    source.unlink()
    public = json.loads(
        (
            await invoke(
                agent_for(kb_dir),
                "get_block_content",
                doc_name="manual",
                blocks=blocks,
                offset=offset,
            )
        ).split("\n\n", 1)[1]
    )
    for payload in (projected, compiled, public):
        assert payload["source_spans"][0][0] == outside
        assert payload["content"].startswith("OUTSIDE")
        assert not payload.get("display_context")


def test_omitted_heading_resets_the_selected_ancestor_path(kb_dir, tmp_path):
    from openkb.block_package import read_block_selection

    text = "# First\n\n" + "a" * 4500 + "\n\n# Second\n\n" + "b" * 2500 + "\n\nOUTSIDE.\n"
    source, _ = freeze_source(kb_dir, tmp_path, "md", text)
    freeze_block_package(source, "manual")
    raw = json.loads(source.with_suffix(".content.json").read_text())
    last = raw["blocks"][-1]["ordinal"]
    selected = read_block_selection(raw, f"1,{last}")
    assert "# Second" not in selected["content"]
    payload = project_evidence(selected, offset=selected["content"].index("OUTSIDE"))
    assert not payload.get("display_context")
    assert payload["content"] == "OUTSIDE.\n"


@pytest.mark.parametrize("extension", ["md", "txt"])
def test_short_import_keeps_qualified_figures_in_both_model_requests(
    kb_dir, tmp_path, model_service, extension
):
    import fitz
    from test_vendor_sdk import response

    from openkb.application.documents import import_document

    picture = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 1, 1), False)
    picture.clear_with(255)
    (tmp_path / "figure.png").write_bytes(picture.tobytes("png"))
    text = "Recorded operating conditions.\n\n![Native caption](figure.png)\n"
    incoming = tmp_path / f"short.{extension}"
    incoming.write_text(text)
    atomic_write_text(
        kb_dir / ".env", f"LLM_API_KEY=fixture\nOPENAI_API_BASE={model_service.url}\n"
    )
    atomic_write_text(
        kb_dir / ".openkb/config.yaml",
        "model: openai/gpt-4o-mini\nlanguage: en\nmodel_policy:\n  retries: 0\n  max_calls: 2\n",
    )
    for output in ({"description": "Source", "content": "# Source\nOriginal."}, {}):
        model_service.replies.append(
            (200, response({"role": "assistant", "content": json.dumps(output)}))
        )
    result = import_document(kb_dir, incoming)
    assert result.status == "added", result.message
    assert len(model_service.requests) == 2
    for request in model_service.requests:
        content = request["messages"][1]["content"]
        body = content if isinstance(content, str) else "".join(item["text"] for item in content)
        structure = json.loads(body.split("Original structure (native-tables-v1):\n", 1)[1])
        assert ("figure_interpretation" in structure) == (extension == "md")
        assert "Native caption" in body


def test_generated_csv_header_requires_its_native_mapping(kb_dir, tmp_path):
    from openkb.source_qualification import source_table_headers

    source, _ = freeze_source(kb_dir, tmp_path, "csv", "one,two\n")
    raw = json.loads(source.with_suffix(".content.json").read_text())
    assert source_table_headers(raw)
    unproved = {**raw, "origins": []}
    assert not source_table_headers(unproved)


def test_legacy_string_source_keeps_literal_tables_unqualified(kb_dir, model_service):
    from test_answer_evidence_regression import selection
    from test_llm_execution import executor_for

    from openkb.agent.compiler import short_document_messages
    from openkb.agent.source_windows import CompileSourceWindows
    from openkb.context.knowledge import KnowledgeRetrieval
    from openkb.context.projection import knowledge_forest

    text = "# shell comment\n\n<table><tr><td>literal example</td></tr></table>\n"
    path = kb_dir / "wiki/sources/legacy.md"
    atomic_write_text(path, text)
    original = path.read_bytes()
    compiled = CompileSourceWindows(kb_dir / "wiki", "legacy").current_original_evidence(text)
    captured = selection(kb_dir)
    reference = next(
        node["source_ref"]
        for document in knowledge_forest(captured)["children"].values()
        for node in [document, *document.get("children", {}).values()]
        if node.get("source_ref")
    )
    public = KnowledgeRetrieval(captured, executor_for(model_service))._read(reference)
    projected = project_evidence({"content": text, "source_spans": [[0, len(text)]]})
    for payload in (compiled, public, projected):
        assert payload["content"] == text
        assert not payload.get("native_tables")
        assert not payload.get("table_interpretation")
    messages = short_document_messages("legacy", text, "schema", "en", original_evidence=compiled)
    content = messages[1]["content"]
    body = content if isinstance(content, str) else "".join(item["text"] for item in content)
    assert not json.loads(body.split("Original structure (native-tables-v1):\n", 1)[1])
    assert path.read_bytes() == original
    assert not path.with_suffix(".content.json").exists()
    assert not model_service.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["br", "div"])
async def test_escaped_pipe_cell_html_preserves_native_roles(kb_dir, tmp_path, tag):
    from openkb.agent.source_windows import CompileSourceWindows

    nested = "\n# Contained example\n\n</div>\n" if tag == "div" else ""
    text = (
        "| Literal | Markup |\r\n| --- | --- |\r\n"
        f"| `escaped \\| <{tag}>` | escaped \\| <{tag}> |\r\n"
        f"\r\n{nested}\n# Native heading\n\nBody\n"
    )
    source, frozen = freeze_source(kb_dir, tmp_path, "md", text)
    package = freeze_block_package(source, "manual")
    parsed = FrozenBlockParser().parse(str(package), images_dir=str(tmp_path / "index/images"))
    assert [h["title"] for node in parsed.nodes for h in node.metadata["headings"]] == [
        "Native heading"
    ]
    from openkb.source_qualification import source_table_headers

    raw = json.loads(source.with_suffix(".content.json").read_text())
    assert len(source_table_headers(raw)) == 1
    offset = frozen.text.index("Body")
    compiled = next(
        CompileSourceWindows(kb_dir / "wiki", "manual").iter_selection(
            "sources/manual.content.json", {"blocks": "1", "offset": offset}, allow_suffix=True
        )
    )
    source.unlink()
    public = json.loads(
        (
            await invoke(
                agent_for(kb_dir), "get_block_content", doc_name="manual", blocks="1", offset=offset
            )
        ).split("\n\n", 1)[1]
    )
    for payload in (compiled, public):
        assert payload["content"] == "Body\n"
        assert [context["text"].strip() for context in payload["display_context"]] == [
            "# Native heading"
        ]
