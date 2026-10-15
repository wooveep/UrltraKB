"""Measure complete project requests before allowing a provider send."""

import json

import pytest


@pytest.mark.parametrize("oversized", ["navigation", "new_page"])
def test_complete_compile_input_never_sends_over_known_capacity(kb_dir, oversized):
    from ultrakb.agent.compile_messages import build_page_messages, planning_messages
    from ultrakb.agent.document_summary import SYSTEM_TEMPLATE, summary_capacity, summary_messages
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.execution_capacity import capacity_policy, require_compile_capacity
    from ultrakb.locks import atomic_write_text
    from ultrakb.model_tokenizer import ContextCapacityError, TokenMeasure

    config = {
        "model": "deepseek/deepseek-flash",
        "model_capacity": {"max_input_tokens": 5000, "tokenizer_model": "gpt-4o-mini"},
    }
    original = "Operating context must be reviewed.\n\n" * (2000 if oversized == "new_page" else 1)
    source = kb_dir / "wiki/sources/manual.md"
    atomic_write_text(source, original)
    summary = kb_dir / "wiki/summaries/manual.md"
    atomic_write_text(summary, "---\nfull_text: sources/manual.md\n---\nPrevious result.")
    before = summary.read_bytes()
    reader = CompileSourceWindows(kb_dir / "wiki", "manual", navigation=[])
    system = {"role": "system", "content": SYSTEM_TEMPLATE.format(language="en")}
    measure = TokenMeasure(capacity_policy(config, custom_endpoint=False))
    fits = summary_capacity(config, custom_endpoint=False)
    windows = list(
        reader.iter_document_windows(fits=lambda w: fits(summary_messages(system, "manual", w)))
    )
    fitting = [
        {
            "messages": summary_messages(system, "manual", w),
            "response_format": {"type": "json_object"},
        }
        for w in windows
    ]
    assert fitting
    for request in fitting:
        require_compile_capacity(measure, request)
    assert "".join(w["content"] for w in windows) == original

    if oversized == "navigation":
        nodes = [
            {"node_id": str(i), "title": f"Operating section {i}", "start_index": 1, "end_index": 1}
            for i in range(1000)
        ]
        messages = planning_messages(
            system,
            {"role": "user", "content": "Generated navigation:\n" + json.dumps(nodes)},
            "Review the operating context.",
            concept_briefs="",
            entity_briefs="",
            entity_types=["other"],
            long=True,
        )
    else:
        selected = reader.prepare({"reading_locations": {"chars": f"0:{len(original)}"}}, long=True)
        messages = build_page_messages(
            system_message=system,
            current_original=None,
            original_context=selected.message,
            known_targets_message={"role": "user", "content": "summaries/manual"},
            task_message={
                "role": "user",
                "content": "Create a concept page for the selected conditions.",
            },
        )
    with pytest.raises(ContextCapacityError, match="Complete compile request"):
        require_compile_capacity(
            measure,
            {"messages": messages, "response_format": {"type": "json_object"}},
        )
    assert summary.read_bytes() == before
    assert source.read_text() == original
