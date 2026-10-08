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
    from openkb.agent.compiler import _page_fields
    from openkb.agent.page_response import PageResponseError

    with pytest.raises(PageResponseError, match=code):
        _page_fields(raw)


@pytest.mark.asyncio
async def test_page_repair_is_local_and_bounded(monkeypatch):
    from openkb.agent.compiler import _llm_call_page_async
    from openkb.compilation_report import collect_compile_report

    calls = []

    async def response(model, messages, step, **kwargs):
        calls.append((step, messages))
        return '{"content":"one"}\n{"note":"two"}' if len(calls) == 1 else '{"content":"repaired"}'

    monkeypatch.setattr("openkb.agent.compiler._llm_call_async", response)
    with collect_compile_report() as report:
        assert (
            json.loads(await _llm_call_page_async("test", [], "entity: API"))["content"]
            == "repaired"
        )
    assert [step for step, _ in calls] == ["entity: API", "entity: API"]
    assert "page_multiple_objects" in calls[1][1][-1]["content"]
    assert report.quality == ["page_response_repaired"] and not report.unfinished


def _entry(number):
    return {
        "structure": str(number),
        "title": str(number),
        "title_origin": "generated",
        "physical_index": number,
        "anchor": {"unit": number, "part": "body", "range": [0, 1], "excerpt": "A"},
    }


def test_full_continuation_is_corrected_without_duplicate_append(monkeypatch):
    from pageindex.index import page_index
    from pageindex.index.page_parts_policy import PagePartsPolicy

    policy = PagePartsPolicy({"unit_count": 103, "page_parts": [{"body": "A", "notes": ""}] * 103})
    previous = [_entry(n) for n in range(1, 52)]
    full = [_entry(n) for n in range(1, 103)]
    incremental = full[51:]
    calls = []

    def response(**kwargs):
        calls.append(kwargs["prompt"])
        return json.dumps(full if len(calls) == 1 else incremental), "finished"

    monkeypatch.setattr(page_index, "llm_completion", response)
    part = "\n".join(f"<physical_index_{n}> A" for n in range(52, 104))
    result = page_index.generate_toc_continue(previous, part, "test", policy)
    assert result == incremental and len(previous) == 51
    assert len(calls) == 2 and "duplicate/conflicting" in calls[-1]
    assert len({item["structure"] for item in previous + result}) == 102


@pytest.mark.parametrize("mode", ["duplicate", "modified", "wrong_anchor", "wrong_page", "order"])
def test_continuation_failure_is_not_silently_deduplicated(monkeypatch, mode):
    from pageindex.index import page_index
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
    calls = []

    def response(**kwargs):
        calls.append(kwargs)
        return json.dumps(added), "finished"

    monkeypatch.setattr(page_index, "llm_completion", response)
    with pytest.raises(PagePartsContractError, match="slide_toc_continue"):
        page_index.generate_toc_continue(previous, "<physical_index_2> A", "test", policy)
    assert len(calls) == 2


@pytest.mark.parametrize("kind", ["corrupt", "scan", "blank"])
def test_unreadable_pdf_never_admits_source(kb_dir, tmp_path, monkeypatch, kind):
    import pymupdf

    from openkb.application.documents import import_document
    from openkb.source_catalog import list_sources

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
    assert result.status == "rejected" and result.message.startswith("PDF导入识别异常")
    assert not list_sources(kb_dir) and result.source_id is None
    assert "pdf_recognition_anomaly" in result.quality


def test_valid_hidden_text_and_blank_page_are_preserved(tmp_path):
    import pymupdf

    from openkb.import_text import require_pdf_text
    from openkb.pdf_recognition import recognize_pdf

    path = tmp_path / "valid.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((30, 30), "Correct hidden OCR text", fill_opacity=0)
        pdf.new_page()
        pdf.save(path)
    require_pdf_text(path)
    parts = recognize_pdf(path)
    assert "Correct hidden OCR text" in parts[0][1]
    assert parts[1] == ("page[2]", "")
