"""Explicit navigation plans retain their original sections and conditions."""

import hashlib
import json

import pytest

from openkb.agent.source_windows import CompileSourceWindows


@pytest.mark.parametrize("previous_page", [None, "concepts/pump.md"])
def test_planned_heading_nodes_include_the_intervening_section(kb_dir, previous_page):
    wiki = kb_dir / "wiki"
    pages = [
        "Preface unrelated to the procedure.",
        "Deployment. Single-controller installation starts here.",
        "Single-controller uses one endpoint.\nDual-controller deployment:",
        "Dual-controller requires two endpoints.\nRemoval:",
        "Remove both endpoints before disconnecting the pump.",
        "Independent maintenance schedule.",
    ]
    (wiki / "sources/current.json").write_text(
        json.dumps(
            [{"page": i, "unit_kind": "page", "content": text} for i, text in enumerate(pages, 1)]
        )
    )
    if previous_page:
        (wiki / previous_page).write_text("# Pump\n\nRetained earlier description.")
    navigation = [
        {
            "node_id": "deployment",
            "start_index": 2,
            "end_index": 2,
            "nodes": [
                {"node_id": "single", "start_index": 2, "end_index": 3},
                {"node_id": "dual", "start_index": 3, "end_index": 4},
            ],
        },
        {
            "node_id": "removal",
            "start_index": 4,
            "end_index": 4,
            "nodes": [{"node_id": "disconnect", "start_index": 4, "end_index": 5}],
        },
    ]
    context = CompileSourceWindows(wiki, "current", navigation=navigation).prepare(
        {"name": "pump", "reading_locations": {"node_ids": ["deployment", "removal"]}},
        previous_page,
        long=True,
    )
    context.require_update("concepts/pump.md")
    content = "".join(window["content"] for window in context.windows)
    native_pages = {span["page"] for window in context.windows for span in window["page_spans"]}
    assert "Single-controller uses one endpoint.\nDual-controller deployment:" in content
    assert "Remove both endpoints before disconnecting" in content
    assert native_pages == {2, 3, 4, 5}
    assert "Preface" not in content and "Independent maintenance" not in content


@pytest.mark.parametrize(
    "node_ids", [["procedure"], ["procedure", "procedure"], ["procedure", "last"]]
)
def test_selected_section_includes_all_descendants_through_bounded_reads(
    kb_dir, monkeypatch, node_ids
):
    wiki = kb_dir / "wiki"
    texts = [
        "Unrelated preface.",
        "Procedure heading.",
        "First condition.\n\n" + "Initial preparation context.\n\n" * 45,
        "Second condition.\n\n" + "Further preparation context.\n\n" * 45,
        "Final child requires supervision.",
        "Unrelated next chapter.",
    ]
    (wiki / "sources/current.json").write_text(
        json.dumps(
            [{"page": i, "unit_kind": "page", "content": text} for i, text in enumerate(texts, 1)]
        )
    )
    navigation = [
        {
            "node_id": "procedure",
            "start_index": 2,
            "end_index": 2,
            "nodes": [
                {"node_id": "first", "start_index": 2, "end_index": 3},
                {"node_id": "repeated-range", "start_index": 2, "end_index": 3},
                {
                    "node_id": "second-heading",
                    "start_index": 3,
                    "end_index": 3,
                    "nodes": [{"node_id": "second", "start_index": 3, "end_index": 4}],
                },
                {"node_id": "last", "start_index": 4, "end_index": 5},
            ],
        }
    ]
    monkeypatch.setattr("openkb.agent.source_windows.MAX_EVIDENCE_CHARS", 600)
    context = CompileSourceWindows(wiki, "current", navigation=navigation).prepare(
        {"name": "pump", "reading_locations": {"node_ids": node_ids}}, long=True
    )
    content = "".join(window["content"] for window in context.windows)
    assert "Final child requires supervision." in content
    assert "Unrelated" not in content
    assert len(context.windows) > 1
    assert all(len(window["content"]) <= 600 for window in context.windows)
    assert context.windows[-1]["continuation"] is None
    for page in (2, 3, 4, 5):
        spans = [
            span
            for window in context.windows
            for span in window["page_spans"]
            if span["page"] == page
        ]
        assert spans[0]["start"] == 0
        assert spans[-1]["end"] == len(texts[page - 1])
        assert all(left["end"] == right["start"] for left, right in zip(spans, spans[1:]))


def test_navigation_ids_are_scoped_to_the_selected_original_version(kb_dir):
    wiki = kb_dir / "wiki"
    windows = []
    for name, start, rule in (
        ("manual-v1", 2, "One endpoint."),
        ("manual-v2", 4, "Two endpoints."),
    ):
        raw = [{"page": i, "unit_kind": "page", "content": "Unrelated."} for i in range(1, 7)]
        raw[start - 1]["content"] = "Deployment heading."
        raw[start]["content"] = rule
        source = wiki / f"sources/{name}.json"
        source.write_text(json.dumps(raw))
        navigation = [
            {
                "node_id": "deployment",
                "start_index": start,
                "end_index": start,
                "nodes": [
                    {"node_id": "requirements", "start_index": start, "end_index": start + 1}
                ],
            }
        ]
        context = CompileSourceWindows(wiki, name, navigation=navigation).prepare(
            {"name": "pump", "reading_locations": {"node_ids": ["deployment"]}}, long=True
        )
        window = context.windows[0]
        assert window["path"] == f"sources/{name}.json"
        assert window["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
        windows.append(window)
    assert [s["page"] for s in windows[0]["page_spans"]] == [2, 3]
    assert [s["page"] for s in windows[1]["page_spans"]] == [4, 5]
    assert (
        "One endpoint." in windows[0]["content"] and "Two endpoints." not in windows[0]["content"]
    )
    assert (
        "Two endpoints." in windows[1]["content"] and "One endpoint." not in windows[1]["content"]
    )


@pytest.mark.parametrize("invalid_range", [(0, 0), (4, 3), (True, 3)])
def test_invalid_descendant_native_ranges_are_rejected(kb_dir, invalid_range):
    from openkb.agent.page_response import PageResponseError

    wiki = kb_dir / "wiki"
    (wiki / "sources/current.json").write_text(
        json.dumps([{"page": 1, "unit_kind": "page", "content": "Procedure heading."}])
    )
    navigation = [
        {
            "node_id": "procedure",
            "start_index": 1,
            "end_index": 1,
            "nodes": [
                {
                    "node_id": "condition",
                    "start_index": invalid_range[0],
                    "end_index": invalid_range[1],
                }
            ],
        }
    ]
    with pytest.raises(PageResponseError, match="Invalid navigation range"):
        CompileSourceWindows(wiki, "current", navigation=navigation).prepare(
            {"name": "pump", "reading_locations": {"node_ids": ["procedure"]}}, long=True
        )


def test_explicit_native_page_ranges_keep_their_exact_selection(kb_dir):
    wiki = kb_dir / "wiki"
    (wiki / "sources/current.json").write_text(
        json.dumps(
            [
                {"page": i, "unit_kind": "page", "content": f"Recorded page {i}."}
                for i in range(1, 5)
            ]
        )
    )
    context = CompileSourceWindows(
        wiki,
        "current",
        navigation=[{"node_id": "whole-chapter", "start_index": 1, "end_index": 4}],
    ).prepare({"name": "pump", "reading_locations": {"pages": "2,4"}}, long=True)
    assert [span["page"] for window in context.windows for span in window["page_spans"]] == [2, 4]


@pytest.mark.parametrize(
    "book_start,selected_range,sibling_range",
    [(1, (3, 3), (7, 10)), (3, (3, 3), (7, 10)), (1, (2, 3), (3, 5))],
)
def test_selected_leaf_does_not_expand_into_ancestors_or_siblings(
    kb_dir, book_start, selected_range, sibling_range
):
    wiki = kb_dir / "wiki"
    (wiki / "sources/current.json").write_text(
        json.dumps(
            [
                {"page": i, "unit_kind": "page", "content": f"Recorded page {i}."}
                for i in range(1, 11)
            ]
        )
    )
    navigation = [
        {
            "node_id": "book",
            "start_index": book_start,
            "end_index": 10,
            "nodes": [
                {
                    "node_id": "selected",
                    "start_index": selected_range[0],
                    "end_index": selected_range[1],
                },
                {
                    "node_id": "sibling",
                    "start_index": sibling_range[0],
                    "end_index": sibling_range[1],
                },
            ],
        }
    ]
    context = CompileSourceWindows(wiki, "current", navigation=navigation).prepare(
        {"name": "pump", "reading_locations": {"node_ids": ["selected"]}}, long=True
    )
    assert [span["page"] for window in context.windows for span in window["page_spans"]] == list(
        range(selected_range[0], selected_range[1] + 1)
    )


def test_disjoint_selected_sections_do_not_read_the_gap(kb_dir):
    wiki = kb_dir / "wiki"
    (wiki / "sources/current.json").write_text(
        json.dumps(
            [
                {"page": i, "unit_kind": "page", "content": f"Recorded page {i}."}
                for i in range(1, 8)
            ]
        )
    )
    navigation = [
        {
            "node_id": name,
            "start_index": start,
            "end_index": start,
            "nodes": [{"node_id": name + "-body", "start_index": start, "end_index": start + 1}],
        }
        for name, start in (("first", 2), ("last", 5))
    ]
    context = CompileSourceWindows(wiki, "current", navigation=navigation).prepare(
        {"name": "pump", "reading_locations": {"node_ids": ["first", "last"]}}, long=True
    )
    assert [span["page"] for window in context.windows for span in window["page_spans"]] == [
        2,
        3,
        5,
        6,
    ]
    assert "Recorded page 4." not in "".join(window["content"] for window in context.windows)


def test_explicit_section_ids_retain_block_coordinates(kb_dir, tmp_path):
    from openkb.block_package import freeze_block_package
    from openkb.inputs import prepared_input
    from openkb.locks import atomic_write_json, atomic_write_text
    from openkb.text_source import freeze_markdown

    labels = (
        "Unrelated preface.",
        "Procedure starts.",
        "Middle condition belongs to this procedure.",
        "Final condition requires supervision.",
        "Unrelated next chapter.",
    )
    text = "".join(label.ljust(1998, "x") + "\n\n" for label in labels)
    original = tmp_path / "manual.md"
    original.write_text(text)
    wiki = kb_dir / "wiki"
    with prepared_input(original) as ready:
        frozen = freeze_markdown(ready, "manual", wiki)
    source = wiki / "sources/manual.md"
    atomic_write_text(source, frozen.text)
    atomic_write_json(source.with_suffix(".content.json"), frozen.model_dump(mode="json"))
    freeze_block_package(source, "manual")
    navigation = [
        {
            "node_id": "procedure",
            "start_index": 2,
            "end_index": 2,
            "nodes": [
                {"node_id": "middle", "start_index": 2, "end_index": 3},
                {"node_id": "last", "start_index": 3, "end_index": 4},
            ],
        }
    ]
    context = CompileSourceWindows(wiki, "manual", navigation=navigation).prepare(
        {"name": "pump", "reading_locations": {"node_ids": ["procedure"]}}, long=True
    )
    assert "".join(window["content"] for window in context.windows) == text[2000:8000]
    assert all("blocks" in window["native_location"] for window in context.windows)
    assert all("pages" not in window["native_location"] for window in context.windows)
    spans = [span for window in context.windows for span in window["source_spans"]]
    assert spans[0][0] == 2000 and spans[-1][1] == 8000
    assert sum(b - a for a, b in spans) == 6000
