"""Necessary selections advance through native readers without another model plan."""

import json

import pytest


def test_explicit_navigation_selection_keeps_all_necessary_pages(kb_dir):
    import json

    from openkb.agent.source_windows import CompileSourceWindows

    wiki = kb_dir / "wiki"
    pages = [
        {"page": i, "unit_kind": "page", "content": f"Required condition {i}."}
        for i in range(1, 13)
    ]
    (wiki / "sources/manual.json").write_text(json.dumps(pages))
    (wiki / "concepts/topic.md").write_text("# Conditions\n\nOld condition.\n")
    nodes = [{"node_id": str(i), "start_index": i, "end_index": i} for i in range(1, 13)]
    windows = CompileSourceWindows(wiki, "manual", navigation=nodes, kb_dir=kb_dir)
    context = windows.prepare(
        {"name": "topic", "reading_locations": {"node_ids": [str(i) for i in range(1, 13)]}},
        "concepts/topic.md",
        long=True,
    )
    body = "".join(w["content"] for w in context.windows)
    assert all(f"Required condition {i}." in body for i in range(1, 13))


def test_necessary_32419_character_selection_reads_every_window_without_plan_repair(kb_dir):
    from openkb.agent.compile_plan import normalize_plan
    from openkb.agent.source_windows import CompileSourceWindows

    wiki = kb_dir / "wiki"
    source = "Initial condition.\n\n" + "x\n\n" * 10780 + "Middle mandatory condition.\n\n"
    source += "z" * (32419 - len(source))
    (wiki / "sources/manual.md").write_text(source)
    (wiki / "concepts/limits.md").write_text("# Limits\n\nOld condition.")
    windows = CompileSourceWindows(wiki, "manual", kb_dir=kb_dir)
    item = {
        "name": "limits",
        "brief": "Retain every required condition",
        "reading_locations": {"chars": "0:32419"},
    }
    plan = normalize_plan(
        {"update": [item]},
        existing={"concepts": {"limits"}, "entities": set()},
        entity_types=frozenset(),
        sanitize=lambda n: n,
    )
    assert windows.validate_updates(plan, long=True) is None
    context = windows.prepare(item, "concepts/limits.md", long=True)
    context.require_update("concepts/limits.md")
    assert "".join(w["content"] for w in context.windows) == source
    assert len(context.windows) > 1
    assert context.windows[0]["continuation"] == context.windows[1]["selection_offset"]


def test_arbitrary_suffix_cannot_certify_the_same_necessary_selection(kb_dir):
    from openkb.agent.page_response import PageResponseError
    from openkb.agent.source_windows import CompileSourceWindows

    wiki = kb_dir / "wiki"
    (wiki / "sources/manual.json").write_text(
        json.dumps(
            [
                {
                    "page": 1,
                    "unit_kind": "page",
                    "content": "Start.\n\n" + "Required middle.\n\n" * 2500 + "Tail.",
                }
            ]
        )
    )
    (wiki / "concepts/limits.md").write_text("# Limits\n\nOld condition.")
    windows = CompileSourceWindows(wiki, "manual")
    with pytest.raises(PageResponseError, match="continuation"):
        windows.prepare(
            {"name": "limits", "reading_locations": {"pages": "1", "offset": 43000}},
            "concepts/limits.md",
            long=True,
        )
