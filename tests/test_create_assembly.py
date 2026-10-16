"""Delivery scope must survive assembly of accepted page contributions."""

import pytest


@pytest.mark.asyncio
async def test_accepted_contributions_keep_local_scope_and_whole_page_coverage(
    tmp_path, monkeypatch
):
    from ultrakb.agent import compiler
    from ultrakb.agent.create_generation import CreationBatch, PageCreation
    from ultrakb.agent.page_figures import PageFigures

    def forbidden(*args, **kwargs):
        pytest.fail("Accepted contributions must not call a model")

    monkeypatch.setattr(compiler, "_llm_call_page_async", forbidden)
    original = "```text\npages 51–105 were not read here\n\tvalue = 1\u00a0+ 2\n```"
    first = (
        "## Deployment\n\nThis contribution reads pages 29–50; "
        "pages 51–105 were not read here.\n\n" + original
    )
    second = (
        "## Lifecycle\n\nThis contribution reads pages 51–105.\n\nA real source limitation remains."
    )
    figures = PageFigures(tmp_path, (), ())
    batches = []
    for i, (start, end, body) in enumerate([(29, 50, first), (51, 105, second)]):
        windows = [
            {
                "path": "sources/manual.json",
                "sha256": "source-version",
                "page_spans": [{"page": p, "start": 0, "end": 10} for p in range(start, end + 1)],
                "native_location": {"pages": ",".join(map(str, range(29, 106)))},
            }
        ]
        batches.append(
            CreationBatch(
                f"create:concepts/lifecycle.md:batch:{i}",
                str(i),
                windows,
                [],
                figures,
                None,
                (),
                {"description": "Lifecycle", "content": body},
            )
        )
    page = PageCreation("concepts/lifecycle.md", "unused", None, None, batches, None)
    result = await page.generate()
    assert "## 本页来源范围" in result["content"]
    assert "物理页 29–105" in result["content"]
    assert "本节来源：" in result["content"]
    assert "物理页 29–50" in result["content"]
    assert "物理页 51–105" in result["content"]
    assert "不表示整页缺少" in result["content"]
    assert first in result["content"] and second in result["content"]
    assert original in result["content"]


def test_source_scope_uses_read_positions_without_filling_page_or_character_gaps():
    from ultrakb.agent.create_assembly import assemble_contributions

    result = assemble_contributions(
        [{"content": "First condition."}, {"content": "Second condition."}],
        [
            [
                {
                    "path": "sources/manual.json",
                    "native_location": {"pages": "1,2,3,4,5,6,7,8,9,10"},
                    "page_spans": [{"page": 2}, {"page": 3}],
                },
                {"path": "sources/note.md", "source_spans": [[0, 10]]},
            ],
            [
                {"path": "sources/manual.json", "page_spans": [{"page": 8}]},
                {"path": "sources/note.md", "source_spans": [[20, 30]]},
            ],
        ],
    )
    whole_page = result.split("---", 1)[0]
    assert "物理页 2–3、8" in whole_page
    assert "字符区间 [0, 10)、[20, 30)" in whole_page
    assert "物理页 1–10" not in result and "物理页 2–8" not in result
    assert "字符区间 [0, 30)" not in result


def test_scope_retains_limits_without_mistaking_partial_windows_for_recognition_gaps():
    from ultrakb.agent.create_assembly import assemble_contributions

    result = assemble_contributions(
        [
            {"content": "Original limitation: OCR could not read the numeric column."},
            {"content": "Tail."},
        ],
        [
            [
                {
                    "path": "sources/slides.json",
                    "unit_kind": "slide",
                    "page_spans": [{"page": 7, "start": 50, "end": 100}],
                    "units": [{"text_coverage": "partial", "structure_coverage": "unconfirmed"}],
                }
            ],
            [{"path": "sources/slides.json", "unit_kind": "slide", "page_spans": [{"page": 8}]}],
        ],
    )
    assert "幻灯片 7–8（所选正文范围）" in result
    assert "含局部正文片段或正文覆盖尚未确认" in result
    assert "正文识别或提取存在缺口" not in result
    assert "结构完整性尚未确认" in result
    assert "Original limitation: OCR could not read the numeric column." in result


def test_scopes_do_not_merge_locations_from_different_original_revisions():
    from ultrakb.agent.create_assembly import assemble_contributions

    result = assemble_contributions(
        [{"content": "Version one."}, {"content": "Version two."}],
        [
            [{"path": "sources/manual.json", "sha256": "a" * 64, "page_spans": [{"page": 1}]}],
            [{"path": "sources/manual.json", "sha256": "b" * 64, "page_spans": [{"page": 2}]}],
        ],
    )
    assert "来源版本 aaaaaaaaaaaa" in result and "来源版本 bbbbbbbbbbbb" in result
    assert "物理页 1–2" not in result


def test_single_contribution_and_its_whitespace_remain_unchanged():
    from ultrakb.agent.create_assembly import assemble_contributions

    body = "\n\nOriginal citation\u00a0\n"
    value = {"content": body}
    assert assemble_contributions([value], [[{"path": "sources/manual.md"}]]) == body
    assert value == {"content": body}


def test_unknown_read_positions_do_not_inherit_a_plan_wide_range():
    from ultrakb.agent.create_assembly import assemble_contributions

    result = assemble_contributions(
        [{"content": "First."}, {"content": "Last."}],
        [[{"path": "sources/manual.json", "native_location": {"pages": "1,2,3"}}], []],
    )
    assert "未记录可显示的位置" in result
    assert "物理页 1–3" not in result
    assert "本节来源：。" not in result
