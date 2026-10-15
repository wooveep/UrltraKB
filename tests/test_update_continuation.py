"""Necessary selections advance through native readers without another model plan."""

import json

import pytest


def test_explicit_navigation_selection_keeps_all_necessary_pages(kb_dir):
    import json

    from ultrakb.agent.source_windows import CompileSourceWindows

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
    from ultrakb.agent.compile_plan import normalize_plan
    from ultrakb.agent.source_windows import CompileSourceWindows

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
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.agent.source_windows import CompileSourceWindows

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


def test_search_hit_reads_the_indexed_section_tail_and_shared_boundary(kb_dir):
    from ultrakb.agent.source_windows import CompileSourceWindows

    wiki = kb_dir / "wiki"
    pages = [
        {"page": 1, "unit_kind": "page", "content": "Aster authentication conditions."},
        {"page": 2, "unit_kind": "page", "content": "Aster authentication renewal."},
        {
            "page": 3,
            "unit_kind": "page",
            "content": "Open the returned address after checking the token.",
        },
        {"page": 4, "unit_kind": "page", "content": "Unrelated maintenance."},
    ]
    (wiki / "sources/manual.json").write_text(json.dumps(pages))
    nodes = [
        {"node_id": "root", "start_index": 1, "end_index": 4},
        {"node_id": "login", "start_index": 1, "end_index": 2},
        {"node_id": "renewal", "start_index": 2, "end_index": 3},
    ]
    windows = CompileSourceWindows(wiki, "manual", navigation=nodes)
    context = windows.prepare({"name": "aster-authentication"}, long=True)
    body = "".join(w["content"] for w in context.windows)
    assert "Open the returned address" in body
    assert "Unrelated maintenance" not in body
    explicit = windows.prepare(
        {"name": "aster-authentication", "reading_locations": {"pages": "2"}}, long=True
    )
    assert "Open the returned address" not in str(explicit.windows)


def test_new_long_page_consumes_the_whole_selected_range(kb_dir):
    from ultrakb.agent.source_windows import CompileSourceWindows

    original = "Initial condition.\n\n" + "Recorded detail.\n\n" * 2000 + "Required tail."
    wiki = kb_dir / "wiki"
    (wiki / "sources/manual.md").write_text(original)
    context = CompileSourceWindows(wiki, "manual").prepare(
        {"name": "new-topic", "reading_locations": {"chars": f"0:{len(original)}"}},
        long=True,
    )
    assert "".join(w["content"] for w in context.windows) == original
    assert len(context.windows) > 1


def test_one_native_section_keeps_its_tail_even_when_it_spans_the_document(kb_dir):
    from ultrakb.agent.source_windows import CompileSourceWindows

    (kb_dir / "wiki/sources/manual.json").write_text(
        json.dumps(
            [
                {"page": 1, "unit_kind": "page", "content": "Authentication starts here."},
                {"page": 2, "unit_kind": "page", "content": "Check the returned token."},
                {"page": 3, "unit_kind": "page", "content": "Then open the returned address."},
            ]
        )
    )
    context = CompileSourceWindows(
        kb_dir / "wiki",
        "manual",
        navigation=[
            {
                "node_id": "authentication",
                "title": "Authentication",
                "start_index": 1,
                "end_index": 3,
            },
        ],
    ).prepare({"name": "authentication"}, long=True)
    body = "".join(w["content"] for w in context.windows)
    assert "Check the returned token." in body
    assert "Then open the returned address." in body
