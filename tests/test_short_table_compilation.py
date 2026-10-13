"""Short import summary and planning see the same original table relationships."""

import json

import pytest
from test_native_table_projection import TABLE

pytest_plugins = ("test_vendor_sdk",)


def test_short_import_projects_tables_into_summary_and_planning(kb_dir, model_service):
    from test_vendor_sdk import response

    from openkb.application.documents import import_document

    (kb_dir / ".env").write_text(f"LLM_API_KEY=fixture\nOPENAI_API_BASE={model_service.url}\n")
    (kb_dir / ".openkb/config.yaml").write_text(
        "model: openai/gpt-4o-mini\nlanguage: en\nmodel_policy:\n  retries: 0\n  max_calls: 2\n"
    )
    original = "\n\n".join(TABLE.replace("Group", f"Group {n}") for n in range(6)) + "\n"
    incoming = kb_dir / "incoming.md"
    incoming.write_text(original)
    for output in ({"description": "Six feature tables", "content": "# Features\nGroups."}, {}):
        model_service.replies.append(
            (200, response({"role": "assistant", "content": json.dumps(output)}))
        )

    result = import_document(kb_dir, incoming)

    assert result.status == "added", result.message
    assert len(model_service.requests) == 2
    originals = list((kb_dir / "wiki/sources").glob("*.md"))
    assert len(originals) == 1 and originals[0].read_text() == original
    for request in model_service.requests:
        content = request["messages"][1]["content"]
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

    from openkb.agent.compiler import short_document_messages
    from openkb.agent.evidence_payload import project_evidence
    from openkb.text_source import read_text_selection

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
