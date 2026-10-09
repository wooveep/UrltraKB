"""Incomplete update evidence cannot replace published knowledge."""

import json

from test_compile_windows import compile_boundary

pytest_plugins = ("test_pdf_readback",)


def test_no_current_long_window_keeps_existing_multisource_page(
    kb_dir, physical_pdf, pdf_model, monkeypatch
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
    compile_boundary(monkeypatch, {"concepts": {"update": [{"name": "orchard"}]}}, calls)
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
    assert result.status == "failed", result
    assert "page_update_originals_unread" in result.quality
    assert (kb_dir / "wiki/concepts/orchard.md").read_bytes() == old
    assert read_head(kb_dir) == head
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
