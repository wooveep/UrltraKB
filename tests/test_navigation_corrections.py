"""Review regressions at the saved PageIndex/transport handoff."""

import json

import pymupdf

from tests.test_navigation_windows import prepare
from tests.test_step_two_formats import saved_views


def test_corrected_middle_start_keeps_original_order(kb_dir, tmp_path, model_service):
    path = tmp_path / "order.md"
    path.write_text("Alpha\n\nBeta\n\nGamma")

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        blocks = payload["evidence"]["blocks"]
        if payload["stage"] == "index_structure":
            return {
                "sections": [
                    {
                        "title": row["text"],
                        "title_origin": "source",
                        "level": 1,
                        "start_block": row["id"] if i != 1 else "missing",
                        "anchor": row["text"],
                    }
                    for i, row in enumerate(blocks)
                ]
            }
        return {
            "locations": [
                {
                    "id": payload["candidates"][0]["id"],
                    "location": {"start_block": blocks[1]["id"], "anchor": "Beta"},
                }
            ]
        }

    model_service.respond = respond
    _, _, _, saved = prepare(kb_dir, path, {})
    assert saved["status"] == "enhanced", saved
    assert [n["title"] for n in saved["nodes"]][1:] == ["Alpha", "Beta", "Gamma"]


def test_repeated_contents_entries_keep_distinct_occurrences(kb_dir, tmp_path, model_service):
    path = tmp_path / "repeated.md"
    path.write_text(
        "# Contents\n\nOverview .... 1\nOverview .... 2\n\n"
        "# Overview\n\nFirst version.\n\n# Overview\n\nSecond version."
    )

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        assert payload["stage"] == "index_location"
        candidates = payload["candidates"]
        assert len(candidates) == 2 and candidates[0]["id"] != candidates[1]["id"]
        headings = [b for b in payload["evidence"]["blocks"] if b["kind"] == "heading"]
        return {
            "locations": [
                {"id": row["id"], "location": {"start_block": block["id"], "anchor": "Overview"}}
                for row, block in zip(candidates, headings)
            ]
        }

    model_service.respond = respond
    _, _, _, saved = prepare(kb_dir, path, {})
    assert saved["status"] == "enhanced", saved
    nodes = saved["nodes"][1:]
    assert [n["title"] for n in nodes] == ["Overview", "Overview"]
    assert nodes[0]["start"] < nodes[1]["start"]


def test_pdf_contents_stops_before_body_pages(kb_dir, tmp_path, model_service):
    path = tmp_path / "contents.pdf"
    pdf = pymupdf.open()
    for text in ("Contents", "Install .... 3", "Install\nUse version 7."):
        page = pdf.new_page()
        page.insert_text((50, 50), text)
    pdf.save(path)
    pdf.close()
    _, parsed, _, saved = prepare(kb_dir, path, {})
    assert saved["status"] == "enhanced", saved
    assert [n["title"] for n in saved["nodes"]][1:] == ["Install"]
    assert saved["nodes"][1]["start"] == next(
        b.order for b in parsed.blocks if b.location["page"] == 3
    )
    assert not model_service


def test_image_bearing_html_retains_heading_and_table(kb_dir, tmp_path):
    path = tmp_path / "image.html"
    (tmp_path / "icon.png").write_bytes(b"frozen image")
    path.write_text(
        '<h1>Install <img src="icon.png"></h1><p>Details</p>'
        '<table><tr><td rowspan="2">A<img src="icon.png"></td><td>B</td>'
        "</tr><tr><td>C</td></tr></table>"
    )
    parsed, _, views = saved_views(kb_dir, path)
    assert parsed.blocks[0].kind == "heading"
    assert parsed.blocks[0].location["heading_level"] == 1
    assert parsed.blocks[2].location["headings"] == ["Install "]
    table = next(v for b, v in zip(parsed.blocks, views) if b.kind == "table")
    assert json.loads(table.text)[0][0]["attributes"]["rowspan"] == "2"
    assert len([b for b in parsed.blocks if b.kind == "image" and b.assets]) == 2


def test_failed_later_window_preserves_native_headings(kb_dir, tmp_path, model_service):
    path = tmp_path / "late.md"
    path.write_text("# First\n\n" + "original " * 300 + "\n\n# Last\n\n" + "tail " * 300)
    calls = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        calls.append(payload)
        if len(calls) > 1:
            return {"invalid": "fail the later window"}
        return {"sections": []}

    model_service.respond = respond
    _, _, _, saved = prepare(kb_dir, path, {"window_tokens": 500})
    assert saved["status"] == "degraded"
    assert {n["title"] for n in saved["nodes"]} >= {"First", "Last"}


def test_pdf_shared_block_headings_preserve_parent_containment(kb_dir, tmp_path, model_service):
    path = tmp_path / "shared.pdf"
    pdf = pymupdf.open()
    for text in ("Alpha", "Beta\nGamma", "Delta"):
        page = pdf.new_page()
        page.insert_text((50, 50), text)
    pdf.save(path)
    pdf.close()

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        blocks = payload["evidence"]["blocks"]
        return {
            "sections": [
                {
                    "title": title,
                    "title_origin": "source",
                    "level": level,
                    "start_block": blocks[order]["id"],
                    "anchor": title,
                }
                for title, level, order in (
                    ("Alpha", 1, 0),
                    ("Beta", 2, 1),
                    ("Gamma", 1, 1),
                    ("Delta", 2, 2),
                )
            ]
        }

    model_service.respond = respond
    source, parsed, _, saved = prepare(kb_dir, path, {})
    assert saved["status"] == "enhanced", saved
    by_title = {row["title"]: row for row in saved["nodes"]}
    assert by_title["Alpha"]["end"] == by_title["Beta"]["end"] == 2
    assert by_title["Gamma"]["start"] == 1
    from openkb.navigation import read_navigation
    from openkb.navigation_tree import block_hints

    restored = read_navigation(kb_dir, source, identity=saved["id"], limit=100)
    assert len(block_hints(restored)) == len(parsed.blocks)


def test_pdf_contents_and_body_can_share_one_page(kb_dir, tmp_path, model_service):
    path = tmp_path / "same-page.pdf"
    pdf = pymupdf.open()
    page = pdf.new_page()
    for i, text in enumerate(("Contents", "Install .... 1", "Install", "Use version 7.")):
        page.insert_text((50, 50 + i * 60), text)
    pdf.save(path)
    pdf.close()
    _, parsed, _, saved = prepare(kb_dir, path, {})
    assert saved["status"] == "enhanced", saved
    assert len(parsed.blocks) == 4
    assert [n["title"] for n in saved["nodes"]][1:] == ["Install"]
    assert saved["nodes"][1]["start"] == 2
    assert not model_service


def test_pdf_bookmarks_take_precedence_over_printed_contents(kb_dir, tmp_path, model_service):
    path = tmp_path / "bookmarks.pdf"
    pdf = pymupdf.open()
    for text in ("Contents\nInstall .... 2\nRepair .... 3", "Install", "Repair"):
        page = pdf.new_page()
        page.insert_text((50, 50), text)
    pdf.set_toc([[1, "Install", 2], [1, "Repair", 3]])
    pdf.save(path)
    pdf.close()
    _, _, _, saved = prepare(kb_dir, path, {})
    assert saved["status"] == "enhanced", saved
    assert [n["title"] for n in saved["nodes"]][1:] == ["Install", "Repair"]
    assert not model_service


def test_unresolved_contents_between_shared_starts_remains_declared(
    kb_dir, tmp_path, model_service
):
    path = tmp_path / "unresolved.pdf"
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((50, 50), "Alpha\nGamma")
    pdf.set_toc([[1, "Alpha", 1], [2, "Missing", 1], [1, "Gamma", 1]])
    pdf.save(path)
    pdf.close()
    stages = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        stages.append(payload["stage"])
        if payload["stage"] == "index_location":
            return {"locations": [{"id": c["id"], "location": None} for c in payload["candidates"]]}
        return {"sections": []}

    model_service.respond = respond
    source, _, _, saved = prepare(kb_dir, path, {})
    assert stages == ["index_location", "index_structure"]
    assert saved["status"] == "degraded", saved
    assert saved["windows"][0]["status"] == "basic"
    assert saved["windows"][0]["unlocated"][0]["title"] == "Missing"
    from openkb.navigation import read_navigation

    restored = read_navigation(kb_dir, source, identity=saved["id"])
    assert restored["windows"] == saved["windows"]
