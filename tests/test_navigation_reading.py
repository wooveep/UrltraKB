"""Reading grouping and bounded recovery use the real saved-parse/index seam."""

import json

import pytest

from openkb.config import load_config, resolve_credential_bundle
from openkb.evidence import BlockDraft
from openkb.locks import kb_ingest_lock
from openkb.navigation import prepare_navigation, read_navigation
from openkb.navigation_evidence import read_evidence_group
from openkb.navigation_metadata import structure_diagnostics
from tests.test_navigation_repairs import saved_parse


def index(kb, source, parsed, *, window_tokens=20000):
    settings = load_config(kb / ".openkb/config.yaml")
    settings["navigation"] = {
        "enabled": True,
        "summaries": False,
        "window_tokens": window_tokens,
    }
    settings["processing"].update(max_attempts=2, output_tokens=4096, max_tokens=1000000)
    with kb_ingest_lock(kb / ".openkb"):
        result = prepare_navigation(
            kb, source, parsed, settings, bundle=resolve_credential_bundle(kb)
        )
    return read_navigation(kb, source, identity=result["id"], limit=200)


@pytest.mark.parametrize("kind,key", [("pdf", "page"), ("pptx", "slide")])
def test_windows_keep_whole_native_units_and_reuse_previous_unit(
    kb_dir, tmp_path, model_service, kind, key
):
    source, parsed = saved_parse(
        kb_dir,
        tmp_path,
        [
            BlockDraft(
                "Ordinary body material. " * 14, "paragraph", {"kind": kind, key: i // 3 + 1}
            )
            for i in range(36)
        ],
    )
    model_service.respond = lambda body: {"sections": []}
    saved = index(kb_dir, source, parsed, window_tokens=2000)
    windows = saved["windows"]
    assert 1 < len(windows) < 12
    for i, window in enumerate(windows):
        assert window["target_start"] % 3 == window["target_end"] % 3 == 0
        assert window["evidence"]["start"] == max(0, window["target_start"] - 3)
        if i:
            assert windows[i - 1]["target_end"] == window["target_start"]
        group = read_evidence_group(kb_dir, source, parsed, window["evidence"])
        assert all(key in block["location"] for block in group["blocks"])
    assert windows[-1]["target_end"] == len(parsed.blocks)


@pytest.mark.parametrize("kind", ["docx", "xlsx"])
def test_table_rows_are_kept_together_without_invented_pages(kb_dir, tmp_path, model_service, kind):
    drafts = []
    for row in range(12):
        for col in range(3):
            location = (
                {"kind": kind, "table": 1, "row": row + 1, "cell": col + 1}
                if kind == "docx"
                else {
                    "kind": kind,
                    "sheet": "Data",
                    "sheet_index": 1,
                    "cell_address": f"{chr(65 + col)}{row + 1}",
                }
            )
            drafts.append(BlockDraft("Related cell contents. " * 14, "table", location))
    source, parsed = saved_parse(kb_dir, tmp_path, drafts)
    model_service.respond = lambda body: {"sections": []}
    saved = index(kb_dir, source, parsed, window_tokens=2000)
    assert len(saved["windows"]) > 1
    assert all(w["target_end"] % 3 == 0 for w in saved["windows"])
    assert all("page" not in block.location for block in parsed.blocks)


def test_oversized_page_splits_at_complete_blocks_without_losing_the_tail(
    kb_dir, tmp_path, model_service
):
    source, parsed = saved_parse(
        kb_dir,
        tmp_path,
        [
            BlockDraft("Long paragraph. " * 40, "paragraph", {"kind": "pdf", "page": 1})
            for _ in range(20)
        ],
    )
    model_service.respond = lambda body: {"sections": []}
    saved = index(kb_dir, source, parsed, window_tokens=1800)
    assert len(saved["windows"]) > 1
    assert [b for w in saved["windows"] for b in range(w["target_start"], w["target_end"])] == list(
        range(20)
    )
    assert all(w["tokens"] <= 1800 for w in saved["windows"])


@pytest.mark.parametrize("recovers", [True, False])
def test_empty_structure_with_title_cues_gets_bounded_review_and_visible_uncertainty(
    kb_dir, tmp_path, model_service, recovers
):
    source, parsed = saved_parse(
        kb_dir,
        tmp_path,
        [
            BlockDraft("7.\nChapter seven", "paragraph", {"kind": "pdf", "page": 11}),
            BlockDraft("Ordinary body content.", "paragraph", {"kind": "pdf", "page": 11}),
        ],
    )
    received = []

    def respond(body):
        task = json.loads(body["messages"][-1]["content"])
        received.append(body)
        if not recovers or "structure_check" not in task:
            return {"sections": []}
        block = task["evidence"]["blocks"][0]
        return {
            "sections": [
                {
                    "title": block["text"],
                    "title_origin": "source",
                    "level": 1,
                    "start_block": block["id"],
                    "anchor": block["text"],
                }
            ]
        }

    model_service.respond = respond
    saved = index(kb_dir, source, parsed)
    assert len(received) == 2
    assert received[0]["messages"][0] == received[1]["messages"][0]
    prefixes = [body["messages"][-1]["content"].split(',"stage":')[0] for body in received]
    assert prefixes[0] == prefixes[1]
    assert saved["nodes"][0]["end"] == len(parsed.blocks)
    if recovers:
        assert saved["status"] == "enhanced"
        assert any(" ".join(n["title"].split()) == "7. Chapter seven" for n in saved["nodes"])
    else:
        assert saved["reason"] == "index_structure_unconfirmed"
        assert structure_diagnostics(saved)[0]["unaccepted_candidates"]["count"] == 1
        assert all(n["structure_origin"] == "basic" for n in saved["nodes"])
    assert index(kb_dir, source, parsed)["id"] == saved["id"]
    assert len(received) == 2


def test_unstructured_prose_does_not_require_headings_or_additional_requests(
    kb_dir, tmp_path, model_service
):
    source, parsed = saved_parse(
        kb_dir,
        tmp_path,
        [
            BlockDraft(
                "A continuous narrative without a heading.",
                "paragraph",
                {"kind": "text", "line": 1},
            ),
            BlockDraft(
                "A later paragraph continues the explanation.",
                "paragraph",
                {"kind": "text", "line": 3},
            ),
        ],
    )
    model_service.respond = lambda body: {"sections": []}
    saved = index(kb_dir, source, parsed)
    assert len(model_service) == 1
    assert saved["status"] == "enhanced"
    assert saved["nodes"][0]["end"] == 2
    assert not structure_diagnostics(saved)


def test_number_only_and_complete_pdf_title_share_one_boundary(kb_dir, tmp_path, model_service):
    source, parsed = saved_parse(
        kb_dir,
        tmp_path,
        [
            BlockDraft("8.\nOperations", "paragraph", {"kind": "pdf", "page": 1}),
            BlockDraft("8.1.\nParameters", "paragraph", {"kind": "pdf", "page": 1}),
            BlockDraft("Parameter details.", "paragraph", {"kind": "pdf", "page": 1}),
        ],
    )

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        assert payload["stage"] == "index_structure"
        blocks = payload["evidence"]["blocks"]
        return {
            "sections": [
                {
                    "title": title,
                    "title_origin": "source",
                    "level": 1,
                    "start_block": blocks[i]["id"],
                    "anchor": anchor,
                }
                for i, title, anchor in (
                    (0, "8.Operations", "Operations"),
                    (1, "8.1.", "8.1."),
                    (1, "8.1.Parameters", "Parameters"),
                )
            ]
        }

    model_service.respond = respond
    saved = index(kb_dir, source, parsed)
    assert len(model_service) == 1
    parent, child = saved["nodes"][1:]
    assert (parent["start"], parent["end"]) == (0, 3)
    assert (child["start"], child["end"], child["parent"]) == (1, 3, parent["id"])
    assert child["structure"]["level"] == 2
    assert "Parameters" in child["title"]


def test_whitespace_tolerance_does_not_accept_a_different_or_partial_title():
    from openkb.navigation_structure import located

    block = {
        "id": "b",
        "kind": "paragraph",
        "text": "9.\nStorage basics",
        "location": {"kind": "pdf", "page": 1},
    }
    row = {
        "title": "9.Storage basics",
        "title_origin": "source",
        "level": 1,
        "start_block": "b",
        "anchor": "Storage basics",
    }
    assert located(row, {"b": block})
    for title in ("8.Storage basics", "Storage", "9.Storage advanced"):
        assert not located({**row, "title": title}, {"b": block})
