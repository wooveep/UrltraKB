"""Incomplete update evidence cannot replace published knowledge."""

import json

import pytest
from test_compile_windows import compile_boundary

pytest_plugins = ("test_pdf_readback",)


@pytest.mark.parametrize(
    "subject,title",
    [("River Valve", "RIVER VALVE"), ("灌溉阀组", "灌溉阀组"), ("Straße", "STRASSE")],
)
def test_long_update_finds_complementary_originals_without_navigation_match(kb_dir, subject, title):
    from openkb.agent.source_windows import CompileSourceWindows

    wiki = kb_dir / "wiki"
    (wiki / "sources/current.json").write_text(
        json.dumps(
            [
                {"page": 1, "unit_kind": "page", "content": "Unrelated general introduction."},
                {"page": 2, "unit_kind": "page", "content": f"{subject}: capacity is 36 litres."},
                {
                    "page": 3,
                    "unit_kind": "page",
                    "content": f"{subject}: only available during irrigation.",
                },
            ]
        )
    )
    context = CompileSourceWindows(
        wiki,
        "current",
        navigation=[
            {"node_id": "root", "title": "Handbook", "start_index": 1, "end_index": 3},
        ],
    ).prepare({"name": subject, "title": title}, long=True)
    context.require_update("entities/valve.md")
    assert "36 litres" in str(context.message)
    assert "only available during irrigation" in str(context.message)
    assert "Unrelated general introduction" not in str(context.message)


@pytest.mark.parametrize("repair", ["retain", "withdraw", "related", "format_then_update"])
def test_no_current_long_window_keeps_existing_multisource_page(
    kb_dir, physical_pdf, pdf_model, monkeypatch, repair
):
    import litellm

    from openkb.application.documents import import_document
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.unit_publication import read_head

    index_response = litellm.completion
    calls = []
    for name, action in (("old-a", "create"), ("old-b", "update")):
        path = kb_dir / f"{name}.txt"
        path.write_text(f"{name}: a supported condition.\n")
        compile_boundary(monkeypatch, {"concepts": {action: [{"name": "orchard"}]}}, calls)
        assert import_document(kb_dir, path).status == "added"
    old = (kb_dir / "wiki/concepts/orchard.md").read_bytes()
    head = read_head(kb_dir)
    apply_kb_config_patch(
        kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"pdf_short_max_pages": 0})
    )
    calls.clear()
    planning = []

    def plan(messages):
        planning.append(messages)
        if repair == "format_then_update":
            return (
                {"concepts": 42}
                if len(planning) == 1
                else {"concepts": {"update": [{"name": "orchard"}]}}
            )
        if len(planning) == 1 or repair == "retain":
            return {"concepts": {"update": [{"name": "orchard"}]}}
        return {} if repair == "withdraw" else {"concepts": {"related": ["orchard"]}}

    compile_boundary(monkeypatch, plan, calls)
    compile_response = litellm.completion

    def routed(**kwargs):
        return (
            compile_response
            if "wiki compilation agent" in str(kwargs["messages"])
            else index_response
        )(**kwargs)

    async def asynchronous(**kwargs):
        return routed(**kwargs)

    monkeypatch.setattr("litellm.completion", routed)
    monkeypatch.setattr("litellm.acompletion", asynchronous)
    result = import_document(kb_dir, physical_pdf)
    if repair == "withdraw":
        assert result.status == "added", result
        assert "compile_plan_update_withdrawn" in result.quality
    else:
        assert result.status == "failed", result
        assert "page_update_originals_unread" in result.quality
    assert (kb_dir / "wiki/concepts/orchard.md").read_bytes() == old
    if repair != "withdraw":
        assert read_head(kb_dir) == head
    assert len(planning) == 2
    assert not any("Update the concept page" in str(call) for call in calls)


def test_partial_prior_window_has_scoped_update_policy_and_continuation(kb_dir, monkeypatch):
    from openkb.agent.source_windows import CompileSourceWindows

    wiki = kb_dir / "wiki"
    (wiki / "sources/current.json").write_text(
        json.dumps(
            [{"page": 1, "unit_kind": "page", "content": "# Irrigation\nOld default is false."}]
        )
    )
    (wiki / "sources/old.md").write_text("Supported arithmetic: 3 * 4 = 12.\n" * 1000)
    (wiki / "concepts/topic.md").write_text(
        "---\nsources: [sources/old.md]\n---\nSupported orchard conditions."
    )
    monkeypatch.setattr("openkb.agent.source_windows.MAX_EVIDENCE_CHARS", 1800)
    context = CompileSourceWindows(wiki, "current").prepare(
        {"name": "topic", "reading_locations": {"pages": "1"}},
        "concepts/topic.md",
        long=True,
    )
    context.require_update("concepts/topic.md")
    assert context.windows[1]["coverage"] == "partial"
    assert context.windows[1]["reading_action"]
    assert context.windows[1]["unread"]
    prompt = str(context.message)
    assert "not evidence of error" in prompt
    assert "explicit counterevidence" in prompt
    assert "supported calculations" in prompt
