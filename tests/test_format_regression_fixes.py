"""Regression boundaries from the real DOCX/PDF and PPTX/PDF comparison."""

import json

import pytest


@pytest.mark.parametrize(
    "raw,code",
    [
        ('{"content":"valid body"}\n{"note":"extra object"}', "page_multiple_objects"),
        ('[{"content":"one"},{"content":"two"}]', "page_multiple_objects"),
        ('{"content":42}', "page_wrong_field_type"),
        ('{"content":"   "}', "page_empty_content"),
        ('{"content":"unfinished', "page_truncated_json"),
        ("", "page_empty_response"),
        ("42", "page_wrong_shape"),
        ("null", "page_wrong_shape"),
    ],
)
def test_invalid_page_response_keeps_its_reason(raw, code):
    from ultrakb.agent.compiler import _page_fields
    from ultrakb.agent.page_response import PageResponseError

    with pytest.raises(PageResponseError, match=code):
        _page_fields(raw)


def test_page_repair_instruction_is_local_and_keeps_its_validation_reason():
    from ultrakb.agent.page_response import PageResponseError, page_repair_message, parse_page

    with pytest.raises(PageResponseError) as invalid:
        parse_page('{"content":"one"}\n{"note":"two"}')
    assert invalid.value.code == "page_multiple_objects"
    correction = page_repair_message(invalid.value.code, "entity: API: " + str(invalid.value))
    assert correction["role"] == "user"
    assert "entity: API" in correction["content"]
    assert "page_multiple_objects" in correction["content"]
    assert "Do not append a second object or note" in correction["content"]
    assert parse_page('{"content":"repaired"}')["content"] == "repaired"


def test_update_repair_keeps_required_evidence_and_only_changes_necessary_blocks():
    from ultrakb.agent.page_response import page_repair_message

    correction = page_repair_message(
        "page_invalid_source_ranges", "Missing original evidence", page_update=True
    )["content"]
    assert "page_invalid_source_ranges" in correction
    assert "captured base_sha256" in correction
    assert "including type and source_ranges when requested" in correction
    assert "only supplied source range IDs" in correction
    assert "every range that supports the returned edits" in correction
    assert "omit unchanged blocks" in correction


def _entry(number):
    return {
        "structure": str(number),
        "title": str(number),
        "title_origin": "generated",
        "physical_index": number,
        "anchor": {"unit": number, "part": "body", "range": [0, 1], "excerpt": "A"},
    }


def test_full_continuation_is_rejected_before_duplicate_append():
    from pageindex.index.page_parts_policy import PagePartsContractError, PagePartsPolicy

    policy = PagePartsPolicy({"unit_count": 3, "page_parts": [{"body": "A", "notes": ""}] * 3})
    previous = [_entry(1)]
    full = [_entry(1), _entry(2), _entry(3)]
    incremental = full[1:]
    part = "<physical_index_2> A\n<physical_index_3> A"
    with pytest.raises(PagePartsContractError, match="duplicate/conflicting"):
        policy.validate_chunk(previous, full, part)
    policy.validate_chunk(previous, incremental, part)
    assert previous == [_entry(1)] and len(incremental) == 2


@pytest.mark.parametrize("mode", ["duplicate", "modified", "wrong_anchor", "wrong_page", "order"])
def test_continuation_failure_is_not_silently_deduplicated(mode):
    from pageindex.index.page_parts_policy import PagePartsContractError, PagePartsPolicy

    policy = PagePartsPolicy({"unit_count": 3, "page_parts": [{"body": "A", "notes": ""}] * 3})
    previous, added = [_entry(1)], [_entry(2)]
    if mode == "duplicate":
        added *= 2
    elif mode == "modified":
        added[0]["structure"] = "1"
    elif mode == "wrong_anchor":
        added[0]["anchor"]["excerpt"] = "invented"
    elif mode == "wrong_page":
        added = [_entry(3)]
    else:
        previous, added = [_entry(3)], [_entry(2)]
    saved = json.dumps(previous)
    with pytest.raises(PagePartsContractError, match="slide_toc_chunk"):
        policy.validate_chunk(previous, added, "<physical_index_2> A")
    assert json.dumps(previous) == saved


@pytest.mark.parametrize("kind", ["corrupt", "scan", "blank"])
def test_unreadable_pdf_distinguishes_corruption_from_retained_no_text(
    kb_dir, tmp_path, monkeypatch, kind
):
    import pymupdf

    from ultrakb.application.documents import import_document
    from ultrakb.source_catalog import list_sources

    path = tmp_path / "problem.pdf"
    if kind == "corrupt":
        path.write_bytes(b"%PDF invalid bytes")
    else:
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            if kind == "scan":
                pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 64, 64), False)
                pix.clear_with(128)
                page.insert_image(pymupdf.Rect(20, 20, 200, 200), stream=pix.tobytes("png"))
            pdf.save(path)
    monkeypatch.setattr(
        "litellm.completion", lambda **kw: pytest.fail("PDF rejection called model")
    )
    result = import_document(kb_dir, path)
    if kind == "corrupt":
        assert result.status == "rejected" and result.message.startswith("PDF导入识别异常")
        assert not list_sources(kb_dir) and result.source_id is None
        assert "pdf_recognition_anomaly" in result.quality
    else:
        assert result.status == "blocked" and "资料已保存，未生成知识" in result.message
        assert len(list_sources(kb_dir)) == 1 and result.source_id
        assert ("blank_original" if kind == "blank" else "no_reliable_text") in result.quality


def test_valid_hidden_text_and_blank_page_are_preserved(tmp_path):
    import pymupdf

    from ultrakb.import_text import require_pdf_text
    from ultrakb.pdf_recognition import recognize_pdf

    path = tmp_path / "valid.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((30, 30), "Correct hidden OCR text", fill_opacity=0)
        pdf.new_page()
        pdf.save(path)
    require_pdf_text(path)
    parts = recognize_pdf(path)
    assert "Correct hidden OCR text" in parts[0][1]
    assert parts[1] == ("page[2]", "")


@pytest.mark.parametrize("oversized_name", ["x" * 100000, "名" * 100000], ids=["ascii", "unicode"])
def test_page_repair_keeps_a_bounded_local_reason_from_large_diagnostics(oversized_name):
    from ultrakb.agent.page_response import page_repair_message

    detail = "entity: API: page_duplicate_field: Duplicate field: " + oversized_name
    detail += " END_OF_COMPLETE_LOCAL_DIAGNOSTIC"
    correction = page_repair_message("page_duplicate_field", detail)["content"]
    assert "entity: API" in correction
    assert "Duplicate field:" in correction
    assert "page_duplicate_field" in correction
    assert "Every JSON object member must occur exactly once" in correction
    assert "END_OF_COMPLETE_LOCAL_DIAGNOSTIC" not in correction
    assert len(correction.encode("utf-8")) < 2000
