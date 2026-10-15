"""Short import summary and planning see the same original table relationships."""

import json

import pytest
from test_native_table_projection import TABLE


def test_short_import_projects_tables_into_summary_and_planning(kb_dir):
    from test_source_qualification import freeze_source

    from ultrakb.agent.compile_messages import planning_messages
    from ultrakb.agent.compiler import short_document_messages
    from ultrakb.agent.evidence_payload import project_evidence
    from ultrakb.text_source import read_text_selection

    original = "\n\n".join(TABLE.replace("Group", f"Group {n}") for n in range(6)) + "\n"
    _, frozen = freeze_source(kb_dir, kb_dir, "md", original)
    evidence = project_evidence(read_text_selection(frozen.model_dump(mode="json")))
    summary_messages = short_document_messages(
        "manual",
        original,
        "",
        "en",
        original_evidence=evidence,
    )
    plan_messages = planning_messages(
        summary_messages[0],
        summary_messages[1],
        "Existing summary for feature tables.",
        concept_briefs="",
        entity_briefs="",
    )
    # Both project stages carry exactly the same native table projection.
    for messages in (summary_messages, plan_messages):
        content = messages[1]["content"]
        text = content if isinstance(content, str) else "".join(item["text"] for item in content)
        assert original in text
        structure = json.loads(text.split("Original structure (native-tables-v1):\n", 1)[1])
        assert len(structure["native_tables"]) == 6
        for n, table in enumerate(structure["native_tables"]):
            cells = {cell["id"]: cell for cell in table["cells"]}
            assert table["rows"][2]["cells"] == ["r2c1", "r2c2", "r3c3"]
            assert cells["r2c1"]["value"] == f"Group {n}"
            assert cells["r2c1"]["rows"] == [2, 4]
            assert cells["r4c3"]["value"] == ""
            start, end = cells["r2c1"]["native_location"]["source_spans"][0]
            assert original[start:end] == f'<td rowspan="3">Group {n}</td>'
    assert (kb_dir / "wiki/sources/manual.md").read_text() == original


@pytest.mark.parametrize(
    "original,has_figure",
    [
        ("![Native caption](images/original.png)", True),
        ("<img src='images/original.png' alt='Native caption'>", True),
        ("`![Example](images/original.png)`", False),
        ("```html\n<img src='images/original.png'>\n```", False),
    ],
)
def test_short_source_projection_distinguishes_image_references_from_examples(
    kb_dir, original, has_figure
):
    from test_original_structure import frozen_text

    from ultrakb.agent.compiler import short_document_messages
    from ultrakb.agent.evidence_payload import project_evidence
    from ultrakb.text_source import read_text_selection

    evidence = project_evidence(read_text_selection(frozen_text(kb_dir, original)))
    content = short_document_messages("manual", original, "", "en", original_evidence=evidence)[1][
        "content"
    ]
    text = "".join(item["text"] for item in content)
    structure = json.loads(text.split("Original structure (native-tables-v1):\n", 1)[1])
    assert original in text
    assert ("figure_interpretation" in structure) is has_figure
    if has_figure:
        assert "Pixels have not been interpreted" in structure["figure_interpretation"]


@pytest.mark.asyncio
async def test_real_short_table_summary_keeps_the_header_value_relationship(
    kb_dir, monkeypatch, live_llm
):
    from test_source_qualification import freeze_source

    from ultrakb.agent.compiler import _llm_call_page_async, short_document_messages
    from ultrakb.agent.evidence_payload import project_evidence
    from ultrakb.agent.page_response import parse_page
    from ultrakb.text_source import read_text_selection

    original = (
        "<table><tr><th>Product</th><th>Credits</th></tr><tr><td>Orion</td><td>17</td></tr></table>"
    )
    _, frozen = freeze_source(kb_dir, kb_dir, "md", original)
    evidence = project_evidence(read_text_selection(frozen.model_dump(mode="json")))
    messages = short_document_messages("manual", original, "", "en", original_evidence=evidence)
    live_llm.install(monkeypatch)
    received = await _llm_call_page_async(
        "deepseek/deepseek-flash",
        messages,
        "summary",
        bundle=live_llm.credentials,
        response_protocol="summary-page-json",
        response_format={"type": "json_object"},
        max_tokens=256,
        numeric_sources=[original],
    )
    content = parse_page(received, strict=True)["content"].lower()
    assert "orion" in content and "17" in content and "credit" in content
    assert "native-tables-v1" in str(live_llm.calls[-1]["messages"])
    assert (kb_dir / "wiki/sources/manual.md").read_text() == original
