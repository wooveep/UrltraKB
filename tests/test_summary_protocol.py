"""Declared summary responses cannot repair missing knowledge into accepted JSON."""

import json
from types import SimpleNamespace

import pytest

from openkb.agent.page_response import PageResponseError


def test_consecutive_source_headings_share_a_body_section():
    from openkb.agent.document_summary import summary_sections

    original = (
        "# Manual\n\n## Conditions\n\nWinter requires supervision.\n\n## Summer\n\nNo heater.\n"
    )
    sections = summary_sections(original)
    assert len(sections) == 2
    assert sections[0]["start"] == 0 and sections[0]["end"] == original.index("## Summer")
    assert sections[-1]["end"] == len(original)


def test_next_block_qualification_is_visible_without_double_primary_coverage(kb_dir):
    from openkb.agent.source_windows import CompileSourceWindows

    rule = "For winter deployments enable heating.\n\n"
    exception = "Exception: insulated indoor deployments do not need heating.\n\n"
    original = rule + exception + "Final operating step.\n"
    (kb_dir / "wiki/sources/manual.md").write_text(original)
    windows = list(
        CompileSourceWindows(kb_dir / "wiki", "manual").iter_document_windows(
            fits=lambda window: len(window["content"]) <= len(rule)
        )
    )
    assert "".join(w["content"] for w in windows) == original
    assert any(exception.strip() in c["content"] for c in windows[0]["supplementary_context"])
    assert all(c["primary"] is False for w in windows for c in w["supplementary_context"])


@pytest.mark.parametrize(
    "raw",
    [
        "# Knowledge\n\nOnly the first condition.",
        '{"description":"x","sections":[{"id":"section-1","content":"line\nunfinished',
        '{"description":"x","sections":[{"id":"section-1","content":"complete"}]',
        '{"description":"x","sections":[{"id":"section-1","content":"unfinished',
    ],
)
def test_summary_rejects_unclosed_or_undeclared_protocol(raw):
    from openkb.agent.page_response import parse_summary

    with pytest.raises(PageResponseError, match="summary_invalid_json"):
        parse_summary(raw, ("section-1",))


def test_summary_receives_exactly_the_expected_nonempty_sections_in_source_order():
    from openkb.agent.page_response import parse_summary

    sections = [
        {"id": "section-2", "content": "Exception: trained technicians only."},
        {"id": "section-1", "content": "Disconnect the load before rewiring."},
    ]
    result = parse_summary(
        json.dumps({"description": "Conditions", "sections": sections}), ("section-1", "section-2")
    )
    assert result["sections"] == list(reversed(sections))
    for invalid in (
        sections[:1],
        sections + sections[:1],
        [sections[0], {"id": "unknown", "content": "x"}],
        [sections[0], {"id": "section-1", "content": " "}],
    ):
        with pytest.raises(PageResponseError):
            parse_summary(
                json.dumps({"description": "Conditions", "sections": invalid}),
                ("section-1", "section-2"),
            )


@pytest.mark.asyncio
async def test_compiler_repairs_only_the_invalid_summary_and_keeps_protocol_off_wire(
    kb_dir, monkeypatch
):
    from openkb.agent.compiler import compile_long_doc

    wiki = kb_dir / "wiki"
    (wiki / "sources/manual.md").write_text(
        "# Operation\n\nDisconnect load before rewiring.\n\n"
        "# Exception\n\nOnly trained technicians may use bench mode.\n"
    )
    summary = wiki / "summaries/manual.md"
    summary.write_text("---\nfull_text: sources/manual.md\n---\nOld successful knowledge.")
    sends = []

    def response(**kwargs):
        sends.append(kwargs)
        assert "response_protocol" not in kwargs and "expected_sections" not in kwargs
        if "Based on the summary above" in str(kwargs["messages"]):
            raw = "{}"
        elif len(sends) == 1:
            raw = "# Premature Markdown answer"
        else:
            batch = json.loads(kwargs["messages"][1]["content"].split("\n", 1)[1])
            raw = json.dumps(
                {
                    "description": "Operation and exception",
                    "sections": [
                        {"id": s["id"], "content": batch["content"][s["start"] : s["end"]]}
                        for s in batch["output_sections"]
                    ],
                }
            )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=raw), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    async def asynchronous(**kwargs):
        return response(**kwargs)

    monkeypatch.setattr("litellm.completion", response)
    monkeypatch.setattr("litellm.acompletion", asynchronous)
    await compile_long_doc("manual", summary, "id", kb_dir, "test-model", navigation=[])
    assert len(sends) == 3  # Invalid summary, its one repair, then the ordinary plan.
    assert "Premature" not in summary.read_text()
    assert "Disconnect load" in summary.read_text()
    assert "Only trained technicians" in summary.read_text()
