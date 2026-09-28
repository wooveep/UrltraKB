"""Regressions from the saved long-PDF navigation diagnosis."""

import json

import pytest

from openkb.config import load_config, resolve_credential_bundle
from openkb.evidence import BlockDraft, Evidence, ParseStore
from openkb.locks import kb_ingest_lock
from openkb.navigation import prepare_navigation, read_navigation
from openkb.navigation_structure import located
from tests.test_native_parsing import source_version
from tests.test_navigation_windows import prepare


@pytest.mark.parametrize("inferred_duplicate", [False, True])
def test_unsorted_starts_keep_valid_items_and_reconcile_unselected_native_parent(
    kb_dir, tmp_path, model_service, inferred_duplicate
):
    source, parsed = saved_parse(
        kb_dir,
        tmp_path,
        [
            BlockDraft("Parent", "paragraph", {"kind": "pdf", "page": 1, "bbox": [0, 0, 100, 20]}),
            BlockDraft("Child", "paragraph", {"kind": "pdf", "page": 1, "bbox": [0, 30, 100, 50]}),
            BlockDraft("Parent", "heading", {"kind": "pdf", "page": 1, "bbox": [0, 0, 100, 20]}),
            BlockDraft("Next", "paragraph", {"kind": "pdf", "page": 2}),
        ],
    )

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "index_location":
            return {"locations": [{"id": c["id"], "location": None} for c in payload["candidates"]]}
        blocks = payload["evidence"]["blocks"]
        rows = [
            {
                "title": blocks[i]["text"],
                "title_origin": "source",
                "level": level,
                "start_block": blocks[i]["id"],
                "anchor": blocks[i]["text"],
            }
            for i, level in ((3, 1), (2, 1), (1, 2))
        ]
        rows.insert(1, {**rows[0], "start_block": "unknown"})
        if inferred_duplicate:
            rows.extend({**row, "title_origin": "inferred"} for row in list(rows[2:]))
        return {"sections": rows}

    model_service.respond = respond
    saved = build(kb_dir, source, parsed)
    parent, child, next_node = saved["nodes"][1:]
    assert [(n["title"], n["start"], n["end"]) for n in saved["nodes"][1:]] == [
        ("Parent", 0, 3),
        ("Child", 1, 3),
        ("Next", 3, 4),
    ]
    assert child["parent"] == parent["id"] and next_node["parent"] == "n0"
    assert all(n["title_origin"] == "source" for n in saved["nodes"][1:])
    assert {a["block_id"] for a in parent["structure"]["anchors"]} == {
        parsed.blocks[0].id,
        parsed.blocks[2].id,
    }
    assert saved["windows"][0]["reason"] == "index_unlocated_sections"
    assert len(model_service) == 2  # Structure plus correction for the unknown block only.


def test_multiline_source_title_requires_the_real_block_and_anchor():
    block = {
        "id": "original",
        "kind": "paragraph",
        "text": "2.\n第二章文件系统的那些事",
        "location": {"kind": "pdf", "page": 25},
    }
    row = {
        "title": "2. 第二章文件系统的那些事",
        "title_origin": "source",
        "level": 1,
        "start_block": "original",
        "anchor": "第二章文件系统的那些事",
    }
    assert located(row, {"original": block})
    assert block["text"] == "2.\n第二章文件系统的那些事"
    for change in (
        {"start_block": "other"},
        {"anchor": "missing"},
        {"anchor": " \n"},
        {"title": "第二章"},
    ):
        assert not located({**row, **change}, {"original": block})
    for role in ("toc", "header", "footer"):
        assert not located(row, {"original": {**block, "location": {"role": role}}})
    for kind in ("image", "metadata"):
        assert not located(row, {"original": {**block, "kind": kind}})


def saved_parse(kb, tmp_path, drafts, *, profile=None):
    path = tmp_path / "saved.txt"
    path.write_text("Saved immutable parser output.")
    source = source_version(kb, path)
    parsed = ParseStore(kb).save(source, profile or {"parser": "navigation-regression"}, drafts)
    return source, parsed


def build(kb, source, parsed):
    settings = load_config(kb / ".openkb/config.yaml")
    settings["navigation"] = {"enabled": True, "summaries": False}
    with kb_ingest_lock(kb / ".openkb"):
        saved = prepare_navigation(
            kb, source, parsed, settings, bundle=resolve_credential_bundle(kb)
        )
    return read_navigation(kb, source, identity=saved["id"], limit=200)


def test_pdf_transcriptions_merge_without_freezing_unknown_ocr_depth(
    kb_dir,
    tmp_path,
    model_service,
):
    texts = ["1.2.2.\n一些有趣的volume", "1.2.2. 一些有趣的 volume", "Body", "2.\n第二章", "Next"]
    source, parsed = saved_parse(
        kb_dir,
        tmp_path,
        [
            BlockDraft(
                text,
                "heading" if i == 1 else "paragraph",
                {
                    "kind": "pdf",
                    "page": 7 if i < 3 else 25,
                    "bbox": [10, 10, 300, 40] if i < 2 else [10, 50 + i * 30, 300, 70 + i * 30],
                },
            )
            for i, text in enumerate(texts)
        ],
    )

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        assert payload["stage"] == "index_structure"
        blocks = payload["evidence"]["blocks"]
        return {
            "sections": [
                {
                    "title": " ".join(blocks[i]["text"].split()),
                    "title_origin": "source",
                    "level": level,
                    "start_block": blocks[i]["id"],
                    "anchor": blocks[i]["text"],
                }
                for i, level in ((0, 3), (3, 1))
            ]
        }

    model_service.respond = respond
    saved = build(kb_dir, source, parsed)
    first, chapter = saved["nodes"][1:]
    assert (first["start"], first["end"], first["parent"]) == (0, 3, "n0")
    assert chapter["parent"] == "n0"
    assert first["structure"]["level"] == 3
    assert {a["block_id"] for a in first["structure"]["anchors"]} == {
        parsed.blocks[0].id,
        parsed.blocks[1].id,
    }
    for block, text in zip(parsed.blocks, texts):
        assert (
            ParseStore(kb_dir)
            .read(
                Evidence(source.source_id, source.id, parsed.id, block.id),
                max_chars=4096,
            )
            .text
            == text
        )


@pytest.mark.parametrize("model_level", [None, 3])
def test_unknown_heading_is_a_flat_hint_and_model_can_supply_its_depth(
    kb_dir,
    tmp_path,
    model_service,
    model_level,
):
    source, parsed = saved_parse(
        kb_dir,
        tmp_path,
        [
            BlockDraft("Unnumbered OCR title", "heading", {"kind": "pdf", "page": 1}),
            BlockDraft("Next section", "paragraph", {"kind": "pdf", "page": 2}),
        ],
    )

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        block = payload["evidence"]["blocks"][1]
        result = {
            "sections": [
                {
                    "title": block["text"],
                    "title_origin": "source",
                    "level": 2,
                    "start_block": block["id"],
                    "anchor": block["text"],
                }
            ]
        }
        if model_level is not None:
            first = payload["evidence"]["blocks"][0]
            result["sections"].insert(
                0,
                {
                    "title": first["text"],
                    "title_origin": "source",
                    "level": model_level,
                    "start_block": first["id"],
                    "anchor": first["text"],
                },
            )
        return result

    model_service.respond = respond
    saved = build(kb_dir, source, parsed)
    unknown, later = saved["nodes"][1:]
    assert unknown["end"] == 1 and later["parent"] == "n0"
    assert unknown["structure"]["level"] == model_level
    assert unknown["structure"]["level_origin"] == ("unknown" if model_level is None else "model")


def test_cross_window_section_is_summarized_with_its_complete_original_range(
    kb_dir,
    tmp_path,
    model_service,
):
    path = tmp_path / "cross-window.md"
    path.write_text(
        "# First\n\n" + "\n\n".join("original " * 80 for _ in range(7)) + "\n\n# Last\n\nTail."
    )
    calls = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        calls.append(payload)
        if payload["stage"] == "index_structure":
            return {"sections": []}
        bounds = [b["block_range"] for b in payload["evidence"]["blocks"]]
        for node in payload["nodes"]:
            assert min(b[0] for b in bounds) <= node["start"]
            assert max(b[1] for b in bounds) >= node["end"]
        return {
            "summaries": [
                {"id": n["id"], "summary": n["title"] + " hint"} for n in payload["nodes"]
            ]
        }

    model_service.respond = respond
    _, _, _, saved = prepare(kb_dir, path, {"summaries": True, "window_tokens": 500})
    assert len(saved["windows"]) > 1
    assert saved["nodes"][1]["end"] > saved["windows"][0]["target_end"]
    assert all(n["summary_origin"] == "model" for n in saved["nodes"][1:])
    assert saved["nodes"][0]["summary"] == ""
    assert saved["capabilities"]["summary_outcomes"] == {"not_requested": 1, "complete": 2}
    assert (
        sum(
            n["title"] == "First"
            for p in calls
            if p["stage"] == "index_summary"
            for n in p["nodes"]
        )
        == 1
    )


def test_summary_suffix_overflow_splits_batches_without_repeating_structure(
    kb_dir,
    tmp_path,
    model_service,
    monkeypatch,
):
    import litellm

    path = tmp_path / "suffix.md"
    path.write_text("# First\n\nOriginal.\n\n# Last\n\nTail.")
    original = litellm.token_counter

    def count(**kwargs):
        messages = kwargs.get("messages")
        if messages:
            payload = json.loads(messages[-1]["content"])
            if payload.get("stage") == "index_summary":
                return 2200 if len(payload["nodes"]) > 1 else 1500
            return 1500
        return original(**kwargs)

    monkeypatch.setattr(litellm, "token_counter", count)
    _, _, _, saved = prepare(
        kb_dir,
        path,
        {
            "summaries": True,
            "execution": {
                "context_tokens": 4096,
                "max_context_tokens": 4096,
                "output_tokens": 2048,
            },
        },
    )
    assert saved["status"] == "enhanced", saved
    assert all(n["summary_origin"] == "model" for n in saved["nodes"][1:])
    payloads = [json.loads(b["messages"][-1]["content"]) for b in model_service]
    assert [p["stage"] for p in payloads] == ["index_structure", "index_summary", "index_summary"]
    assert all(len(p["nodes"]) == 1 for p in payloads[1:])
    assert all(p["evidence"] == payloads[0]["evidence"] for p in payloads[1:])


def test_window_budget_includes_known_structure_diagnostics(
    kb_dir, tmp_path, model_service, monkeypatch
):
    import litellm

    path = tmp_path / "diagnostics.md"
    path.write_text("# First\n\nOriginal.\n\n# Last\n\nTail.")
    issue = {
        "entry": 0,
        "level": 1,
        "toc_block": None,
        "start": 0,
        "end": 4,
        "title": "Unlocated title",
        "reason": "index_unlocated_contents",
    }
    monkeypatch.setattr(
        "openkb.navigation_toc.directory_sections",
        lambda *a, **k: ([], set(), [(0, 4)], [issue]),
    )
    original = litellm.token_counter

    def count(**kwargs):
        if kwargs.get("messages"):
            p = json.loads(kwargs["messages"][-1]["content"])
            return (
                2200
                if (
                    p.get("stage") == "index_summary"
                    and p.get("structure_issues")
                    and len(p["evidence"]["blocks"]) > 2
                )
                else 1500
            )
        return original(**kwargs)

    monkeypatch.setattr(litellm, "token_counter", count)
    prepare(
        kb_dir,
        path,
        {
            "summaries": True,
            "execution": {
                "context_tokens": 4096,
                "max_context_tokens": 4096,
                "output_tokens": 2048,
            },
        },
    )
    payloads = [json.loads(b["messages"][-1]["content"]) for b in model_service]
    structures = [p for p in payloads if p["stage"] == "index_structure"]
    summaries = [p for p in payloads if p["stage"] == "index_summary"]
    assert len(structures) >= 2 and summaries
    assert all(any(p["evidence"] == s["evidence"] for s in structures) for p in summaries)


@pytest.mark.parametrize("missing", [False, True])
def test_oversized_section_records_actual_summary_coverage(
    kb_dir,
    tmp_path,
    model_service,
    monkeypatch,
    missing,
):
    import litellm

    path = tmp_path / "oversized.md"
    path.write_text("# First\n\n" + "\n\n".join(f"Paragraph {i}." for i in range(5)))
    original = litellm.token_counter
    requested = []

    def count(**kwargs):
        messages = kwargs.get("messages")
        if messages:
            payload = json.loads(messages[-1]["content"])
            if payload.get("stage") == "index_summary":
                return 2200 if len(payload["evidence"]["blocks"]) > 2 else 1500
            return 1500
        return original(**kwargs)

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "index_structure":
            return {"sections": []}
        node = payload["nodes"][0]
        requested.append([node["start"], node["end"]])
        return {
            "summaries": [
                {
                    "id": node["id"],
                    "summary": None if missing and node["start"] == 2 else "Original hint.",
                }
            ]
        }

    monkeypatch.setattr(litellm, "token_counter", count)
    model_service.respond = respond
    source, _, _, saved = prepare(
        kb_dir,
        path,
        {
            "summaries": True,
            "execution": {
                "context_tokens": 4096,
                "max_context_tokens": 4096,
                "output_tokens": 2048,
            },
        },
    )
    assert requested == [[0, 2], [2, 4], [4, 6]]
    restored = read_navigation(kb_dir, source, identity=saved["id"])
    details = restored["nodes"][1]["summary_details"]
    assert details["status"] == ("partial" if missing else "complete")
    assert details["covered_ranges"] == ([[0, 2], [4, 6]] if missing else [[0, 6]])
    assert details["reason"] == ("index_summary_empty" if missing else None)
    if not missing:
        from copy import deepcopy

        from openkb.navigation_tree import validate_nodes

        forged = deepcopy(restored["nodes"])
        forged[1]["summary_details"]["covered_ranges"] = [[0, 2]]
        with pytest.raises(ValueError, match="Incomplete navigation summary coverage"):
            validate_nodes(forged, 6)


def test_unlocated_bookmark_group_does_not_leave_phantom_depth(kb_dir, tmp_path, model_service):
    source, parsed = saved_parse(
        kb_dir,
        tmp_path,
        [
            BlockDraft("Install", "paragraph", {"kind": "pdf", "page": 1}),
            BlockDraft("Repair", "paragraph", {"kind": "pdf", "page": 2}),
        ],
        profile={
            "parser": "navigation-regression",
            "toc": [
                {"title": "Grouping label", "level": 1, "physical_page": 1},
                {"title": "Install", "level": 2, "physical_page": 1},
                {"title": "Repair", "level": 2, "physical_page": 2},
            ],
        },
    )

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "index_location":
            return {"locations": [{"id": c["id"], "location": None} for c in payload["candidates"]]}
        return {"sections": []}

    model_service.respond = respond
    saved = build(kb_dir, source, parsed)
    assert [n["structure"]["level"] for n in saved["nodes"][1:]] == [1, 1]
    assert all(n["parent"] == "n0" for n in saved["nodes"][1:])


@pytest.mark.parametrize(
    "location",
    [
        {"kind": "pdf", "page": 2, "bbox": [10, 10, 300, 40]},
        {"kind": "pdf", "page": 1, "bbox": [10, 60, 300, 90]},
        {"kind": "pdf", "page": 1},
    ],
)
def test_equal_titles_at_unproven_or_distinct_positions_stay_distinct(
    kb_dir,
    tmp_path,
    model_service,
    location,
):
    source, parsed = saved_parse(
        kb_dir,
        tmp_path,
        [
            BlockDraft(
                "Repeated title",
                "heading",
                {
                    "kind": "pdf",
                    "page": 1,
                    "bbox": [10, 10, 300, 40],
                },
            ),
            BlockDraft("Repeated title", "heading", location),
        ],
    )
    model_service.respond = lambda _: {"sections": []}
    saved = build(kb_dir, source, parsed)
    assert len(saved["nodes"]) == 3
    assert [n["start"] for n in saved["nodes"][1:]] == [0, 1]


def test_indivisible_summary_failure_keeps_other_ranges_and_exposes_budget_reason(
    kb_dir,
    tmp_path,
    model_service,
    monkeypatch,
):
    import litellm

    path = tmp_path / "indivisible.md"
    path.write_text("# First\n\nINDIVISIBLE original text.\n\n# Last\n\nTail.")
    original = litellm.token_counter

    def count(**kwargs):
        messages = kwargs.get("messages")
        if messages:
            payload = json.loads(messages[-1]["content"])
            if payload.get("stage") == "index_summary" and any(
                "INDIVISIBLE" in b["text"] for b in payload["evidence"]["blocks"]
            ):
                return 2200
            return 1500
        return original(**kwargs)

    monkeypatch.setattr(litellm, "token_counter", count)
    _, _, _, saved = prepare(
        kb_dir,
        path,
        {
            "summaries": True,
            "execution": {
                "context_tokens": 4096,
                "max_context_tokens": 4096,
                "output_tokens": 2048,
            },
        },
    )
    first, last = saved["nodes"][1:]
    assert saved["status"] == "degraded"
    assert first["summary_details"] == {
        "status": "partial",
        "reason": "input_budget_exceeded",
        "covered_ranges": [[0, 1]],
        "basis": "original",
    }
    assert last["summary_details"]["status"] == "complete"
    # Actual summary budgeting closes W before the indivisible block.
    assert len(model_service) == 5  # Three structure windows and two fitting summaries.


def test_bookmark_matching_does_not_use_a_running_header(kb_dir, tmp_path, model_service):
    source, parsed = saved_parse(
        kb_dir,
        tmp_path,
        [
            BlockDraft("Install", "heading", {"kind": "pdf", "page": 1, "role": "header"}),
            BlockDraft("Install", "paragraph", {"kind": "pdf", "page": 1}),
            BlockDraft("Original instructions", "paragraph", {"kind": "pdf", "page": 1}),
        ],
        profile={
            "parser": "navigation-regression",
            "toc": [
                {"title": "Install", "level": 1, "physical_page": 1},
            ],
        },
    )
    saved = build(kb_dir, source, parsed)
    assert not model_service
    assert [(n["title"], n["start"]) for n in saved["nodes"][1:]] == [("Install", 1)]


def test_output_truncation_is_a_budget_outcome_not_an_execution_interrupt(
    kb_dir,
    tmp_path,
    model_service,
):
    path = tmp_path / "truncated-summary.md"
    path.write_text("# First\n\nOriginal instructions.")
    model_service.finish_reason = "length"
    _, _, _, saved = prepare(kb_dir, path, {"summaries": True})
    assert saved["status"] == "degraded"
    assert saved["nodes"][1]["summary_details"] == {
        "status": "budget_exceeded",
        "reason": "output_budget_exhausted",
        "covered_ranges": [],
    }
