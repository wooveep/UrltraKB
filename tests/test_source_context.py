"""Original context and reader metadata remain distinct across public ingestion."""

import json

import pytest

from openkb.application.documents import import_document
from tests.document_fixtures import write_docx
from tests.http_model_fixture import evidence_response


def test_docx_table_context_keeps_original_cells_separate_from_reader_status(
    kb_dir, tmp_path, model_service
):
    source = tmp_path / "retention.docx"
    write_docx(
        source,
        "<w:tbl>"
        + "".join(
            "<w:tr>"
            + "".join(f"<w:tc><w:p><w:r><w:t>{v}</w:t></w:r></w:p></w:tc>" for v in row)
            + "</w:tr>"
            for row in [("Item", "Retention"), ("header role unconfirmed", "7 days")]
        )
        + "</w:tbl>",
    )
    observed = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] in {"facts", "generation", "verification"}:
            observed.append(payload)
        return evidence_response(payload)

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.status == "added" and result.knowledge_compilation == "completed", result
    assert {p["stage"] for p in observed} == {"facts", "generation", "verification"}
    for payload in observed:
        rows = payload["units"] if payload["stage"] == "facts" else payload["evidence"]
        cell = next(row for row in rows if row["text"] == "7 days")
        assert "context" not in cell, "Mixed display text must not compete with typed context."
        assert cell["context_data"]["reader_status"] == {"header_role": "unconfirmed"}
        assert cell["context_data"]["structure"] == {"table": 1, "colspan": 1, "rowspan": 1}
        assert cell["context_data"]["source_excerpts"] == [
            {"text": "Item", "row": 1, "cell": 1, "relation": "first_row"},
            {"text": "Retention", "row": 1, "cell": 2, "relation": "first_row"},
        ]
        assert any(row["text"] == "header role unconfirmed" for row in rows)


def test_structured_table_context_pages_through_query_tools_without_duplicate_metadata(
    kb_dir, tmp_path, model_service
):
    import asyncio

    from agents.tool_context import ToolContext

    from openkb.agent.source_tools import source_tools
    from openkb.application.source_actions import read_source_evidence
    from openkb.evidence import Evidence, ParseStore, complete_read_bound

    source = tmp_path / "long-header.docx"
    header = "Exact retention conditions. " * 40
    write_docx(
        source,
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>" + header + "</w:t></w:r></w:p></w:tc></w:tr>"
        "<w:tr><w:tc><w:p><w:r><w:t>7 days</w:t></w:r></w:p></w:tc></w:tr></w:tbl>",
    )
    imported = import_document(kb_dir, source)
    assert imported.knowledge_compilation == "completed", imported
    parsed = ParseStore(kb_dir).load(imported.parse_id)
    block = parsed.blocks[-1]
    reference = Evidence(imported.source_id, imported.input_version, parsed.id, block.id)
    before = read_source_evidence(kb_dir, reference, max_chars=complete_read_bound(block))
    tools = {tool.name: tool for tool in source_tools(kb_dir)[0]}

    def invoke(name, **args):
        return json.loads(
            asyncio.run(
                tools[name].on_invoke_tool(
                    ToolContext(
                        context=None, tool_name=name, tool_call_id="test", tool_arguments="{}"
                    ),
                    json.dumps(args),
                )
            )
        )

    tree = invoke("read_source_tree", source_id=imported.source_id)
    node = next(node for node in tree["nodes"] if node["start"] <= block.order < node["end"])
    cursor = {"offset": block.order - node["start"], "start": 0}
    contexts, texts = [], []
    for _ in range(30):
        result = invoke(
            "read_source_node",
            source_id=imported.source_id,
            node_id=node["id"],
            max_chars=128,
            **cursor,
        )
        row = result["evidence"][0]
        assert row["context_format"] == "structured_json"
        assert "context_data" not in row
        assert len(row["text"]) + len(row["context"]) <= 128
        assert row["context_start"] == sum(map(len, contexts))
        texts.append(row["text"])
        contexts.append(row["context"])
        if result["next"] is None:
            assert row["context_complete"]
            break
        assert not row["context_complete"]
        cursor = result["next"]
    else:
        raise AssertionError("The context cursor did not finish")
    assert len(contexts) > 1
    assert "".join(texts) == before.text == "7 days"
    assert json.loads("".join(contexts)) == before.context_data
    assert before.context_data["source_excerpts"][0]["text"] == header
    assert "header role unconfirmed" in before.context
    before.context_data["source_excerpts"][0]["text"] = "mutated by caller"
    assert read_source_evidence(
        kb_dir, reference, max_chars=complete_read_bound(block)
    ).context_data == json.loads("".join(contexts))
    matching = invoke("search_source_text", source_id=imported.source_id, query="7 days")
    assert json.loads(matching["evidence"][0]["context"]) == json.loads("".join(contexts))


def test_legacy_parse_identity_and_evidence_survive_typed_context_upgrade(kb_dir, tmp_path):
    from openkb.evidence import BlockDraft, Evidence, ParseStore
    from openkb.locks import atomic_write_json
    from openkb.sources import SourceStore, content_id
    from tests.test_source_evidence import save_source

    path = tmp_path / "historical.txt"
    path.write_text("Original cell")
    source = save_source(kb_dir, path)
    store = ParseStore(kb_dir)
    # This is the pre-context_data on-disk format, independent of the new serializer.
    old_block = {
        "order": 0,
        "blob": SourceStore(kb_dir).put_bytes(b"Original cell"),
        "chars": 13,
        "kind": "table",
        "location": {"kind": "docx", "table": 1},
        "assets": [],
        "context": "Table 1; original display context",
    }
    old_block = {"id": content_id(old_block), **old_block}
    profile = {"parser": "historical-reader"}
    record = {
        "input_key": source.input_key,
        "lookup_key": content_id({"input": source.input_key, "profile": profile}),
        "profile": profile,
        "blocks": [old_block],
        "quality": [],
    }
    identity = content_id(record)
    artifact = store.root / f"{identity}.json"
    atomic_write_json(artifact, {"id": identity, **record})
    old_bytes = artifact.read_bytes()
    parsed = store.load(identity)
    ref = Evidence(source.source_id, source.id, identity, old_block["id"])
    view = store.read(ref, max_chars=100)
    assert view.text == "Original cell" and view.context == old_block["context"]
    assert view.context_data is None
    saved = store.save(
        source, profile, [BlockDraft(view.text, view.kind, view.location, context=view.context)]
    )
    assert saved == parsed and artifact.read_bytes() == old_bytes


def test_declared_header_in_embedded_docx_retains_original_attachment_positions(kb_dir, tmp_path):
    from openkb.evidence import Evidence, ParseStore
    from openkb.parsing import parse_document
    from tests.docx_attachment_fixtures import attached_docx
    from tests.test_source_evidence import save_source

    child = tmp_path / "child.docx"
    write_docx(
        child,
        "<w:tbl><w:tr><w:trPr><w:tblHeader/></w:trPr>"
        "<w:tc><w:p><w:r><w:t>Retention</w:t></w:r></w:p></w:tc></w:tr>"
        "<w:tr><w:tc><w:p><w:r><w:t>7 days</w:t></w:r></w:p></w:tc></w:tr></w:tbl>",
    )
    parent = attached_docx(tmp_path / "parent.docx", child.read_bytes())
    source = save_source(kb_dir, parent)
    parsed = parse_document(kb_dir, source)
    store = ParseStore(kb_dir)
    views = [
        store.read(Evidence(source.source_id, source.id, parsed.id, block.id), max_chars=4000)
        for block in parsed.blocks
    ]
    view = next(view for view in views if view.text == "7 days")
    assert view.location["attachment"]["part"] == "word/embeddings/object.bin"
    position = view.location["attachment"]["position"]
    assert position["row"] == 2 and position["cell"] == 1
    assert view.context_data["source_excerpts"] == [
        {"text": "Retention", "row": 1, "cell": 1, "relation": "declared_header"}
    ]
    assert view.context_data["reader_status"] == {"header_role": "declared"}


@pytest.mark.parametrize("number_format, ordered", [("decimal", True), ("bullet", False)])
def test_table_list_levels_are_normalized_at_the_docx_boundary(
    kb_dir, tmp_path, model_service, number_format, ordered
):
    from openkb.evidence import ParseStore
    from tests.docx_attachment_fixtures import docx_with_parts

    source = docx_with_parts(
        tmp_path / "numbered-table.docx",
        '<w:tbl><w:tr><w:tc><w:p><w:pPr><w:numPr><w:ilvl w:val="0"/>'
        '<w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t>Close before cleaning.</w:t>'
        "</w:r></w:p></w:tc></w:tr></w:tbl>",
        parts={
            "word/numbering.xml": '<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/>'
            f'<w:numFmt w:val="{number_format}"/><w:lvlText w:val="%1."/></w:lvl></w:abstractNum>'
            '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>'
        },
        relationships=(
            '<Relationship Id="numbering" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" '
            'Target="numbering.xml"/>'
        ),
    )
    result = import_document(kb_dir, source)
    assert result.status == "added" and result.knowledge_compilation == "completed", result
    parsed = ParseStore(kb_dir).load(result.parse_id)
    assert parsed.blocks[0].context_data["structure"]["list_level"] == 0
    assert parsed.blocks[0].context_data["structure"]["ordered"] is ordered


@pytest.mark.parametrize("span", [0, -1])
def test_invalid_table_span_becomes_a_local_structure_gap(kb_dir, tmp_path, model_service, span):
    from openkb.evidence import ParseStore

    source = tmp_path / "bad-span.docx"
    write_docx(
        source,
        "<w:p><w:r><w:t>Independent instruction: disconnect before cleaning.</w:t></w:r></w:p>"
        f'<w:tbl><w:tr><w:tc><w:tcPr><w:gridSpan w:val="{span}"/></w:tcPr>'
        "<w:p><w:r><w:t>Printed table text.</w:t></w:r></w:p></w:tc></w:tr></w:tbl>",
    )
    result = import_document(kb_dir, source)
    assert result.status == "added" and result.knowledge_compilation == "completed", result
    parsed = ParseStore(kb_dir).load(result.parse_id)
    assert len(parsed.blocks) == 2
    assert "docx_table_span_omitted" in result.warnings
    assert any(row["reason"] == "docx_table_span_omitted" for row in result.coverage["issues"])
    assert "colspan" not in parsed.blocks[1].context_data["structure"]
    assert parsed.blocks[1].context_data["structure"]["rowspan"] == 1


def test_table_context_separates_generated_note_notices_from_literal_original_words(
    kb_dir, tmp_path, model_service
):
    from openkb.evidence import ParseStore

    source = tmp_path / "note-header.docx"
    literal = "The printed label is [footnote 7: unresolved]."
    write_docx(
        source,
        '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Retention</w:t><w:footnoteReference w:id="7"/>'
        "</w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>" + literal + "</w:t></w:r></w:p></w:tc></w:tr>"
        "<w:tr><w:tc><w:p><w:r><w:t>7 days</w:t></w:r></w:p></w:tc></w:tr></w:tbl>",
    )
    result = import_document(kb_dir, source)
    assert result.status == "added" and result.knowledge_compilation == "completed", result
    parsed = ParseStore(kb_dir).load(result.parse_id)
    excerpts = parsed.blocks[-1].context_data["source_excerpts"]
    assert excerpts == [
        {"text": "Retention", "row": 1, "cell": 1, "relation": "first_row"},
        {"text": literal, "row": 1, "cell": 2, "relation": "first_row"},
    ]
    assert any(row["reason"] == "unresolved_docx_note" for row in parsed.quality)
    for request in model_service:
        payload = json.loads(request["messages"][-1]["content"])

        def check(value):
            if isinstance(value, dict):
                if isinstance(value.get("source_excerpts"), list):
                    for excerpt in value["source_excerpts"]:
                        assert excerpt["text"] != "Retention[footnote 7: unresolved]"
                for item in value.values():
                    check(item)
            elif isinstance(value, list):
                for item in value:
                    check(item)

        check(payload)


def test_table_context_preserves_resolved_note_wording_with_its_original_role(kb_dir, tmp_path):
    from openkb.parsing import parse_document
    from tests.test_source_evidence import save_source

    source = tmp_path / "note-conditions.docx"
    write_docx(
        source,
        '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Retention</w:t><w:footnoteReference w:id="7"/>'
        "</w:r></w:p></w:tc></w:tr>"
        "<w:tr><w:tc><w:p><w:r><w:t>7 days</w:t></w:r></w:p></w:tc></w:tr></w:tbl>",
        footnotes=(
            '<w:footnote w:id="7"><w:p><w:r><w:t>Except disputed records.'
            "</w:t></w:r></w:p></w:footnote>"
        ),
    )
    version = save_source(kb_dir, source)
    parsed = parse_document(kb_dir, version)
    assert parsed.blocks[-1].context_data["source_excerpts"] == [
        {"text": "Retention", "row": 1, "cell": 1, "relation": "first_row"},
        {
            "text": "Except disputed records.",
            "source_kind": "footnote",
            "row": 1,
            "cell": 1,
            "relation": "first_row",
        },
    ]


@pytest.mark.parametrize("in_note", [False, True])
def test_table_excerpts_never_join_paragraphs_into_a_new_number(kb_dir, tmp_path, in_note):
    from openkb.parsing import parse_document
    from tests.test_source_evidence import save_source

    source = tmp_path / "separate-numbers.docx"
    paragraphs = (
        "<w:p><w:r><w:t>Minimum: 1</w:t></w:r></w:p><w:p><w:r><w:t>0 degrees</w:t></w:r></w:p>"
    )
    header = '<w:p><w:r><w:footnoteReference w:id="7"/></w:r></w:p>' if in_note else paragraphs
    write_docx(
        source,
        "<w:tbl><w:tr><w:tc>" + header + "</w:tc></w:tr>"
        "<w:tr><w:tc><w:p><w:r><w:t>Rule</w:t></w:r></w:p></w:tc></w:tr></w:tbl>",
        footnotes='<w:footnote w:id="7">' + paragraphs + "</w:footnote>" if in_note else None,
    )
    version = save_source(kb_dir, source)
    parsed = parse_document(kb_dir, version)
    assert parsed.blocks[-1].context_data["source_excerpts"] == [
        {
            "text": text,
            "row": 1,
            "cell": 1,
            "relation": "first_row",
            **({"source_kind": "footnote"} if in_note else {}),
        }
        for text in ("Minimum: 1", "0 degrees")
    ]
