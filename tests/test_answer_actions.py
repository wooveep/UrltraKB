"""Count actual rendered glyph clusters and retain exact answer/usage ownership."""

import json
import os
import subprocess
import sys

import pytest

pytest.importorskip("PySide6")

from ultrakb.desktop.answer_actions import character_count


@pytest.mark.parametrize(
    "text,expected",
    [
        ("中文，English!", 11),
        ("a b\n\t中\u00a0文", 4),
        ("e\u0301", 1),
        ("👨‍👩‍👧‍👦🇨🇳👍🏽1️⃣", 4),
        ("\ufffc", 0),
        ("x²+y²=1", 7),
        ("🇨🇳🇺🇸🇦", 3),
    ],
)
def test_grapheme_count(text, expected):
    assert character_count(text) == expected


def test_markdown_counts_rendered_text_including_code_and_tables():
    from PySide6.QtGui import QTextDocument

    from ultrakb.desktop.verification import create_application
    from ultrakb.rendering.markdown import render_markdown

    app = create_application()
    document = QTextDocument()
    document.setHtml(
        render_markdown(
            "# 标题\n\n**粗体** [链接](https://example.invalid)\n\n"
            "```text\na + b\n```\n\n|列|值|\n|--|--|\n|中|2|",
            None,
        ).html
    )
    assert character_count(document.toPlainText()) == 13
    assert app is not None


def test_single_usage_persists_without_using_cumulative_and_notices_have_no_answer(kb_dir):
    from ultrakb.application.conversations import read_conversation
    from ultrakb.application.views import published_scope
    from ultrakb.storage.session_result import reference_trace
    from ultrakb.storage.session_store import SessionStore

    store = SessionStore(kb_dir, published_scope(kb_dir).knowledge.identity)
    store.create("ui-sample", model="stored-ui-sample", language="zh")
    usage = {"input_cached": 100, "input_uncached": 20, "output": 10, "reasoning": 2}
    answer = "# 第一条\n\n[[summaries/policy]]"
    store.append_exchange(
        "ui-sample",
        "first-turn",
        "first",
        answer,
        metadata={
            "schema": 1,
            "trace": reference_trace([], answer),
            "result": {"usage": usage},
        },
    )
    store.append_exchange("ui-sample", "second-turn", "second", "没有已有单条用量。")
    store.queue_submission("ui-sample", "unfinished", "尚未完成", view_id="legacy", after_turn=2)
    view = read_conversation(kb_dir, "ui-sample")
    assert view.answers[0].usage == usage and view.answers[1].usage is None
    assert view.timeline_answers[-1] is None
    assert view.answers[0].markdown.endswith("[[summaries/policy]]")
    assert view.answers[0].identity == store.load("ui-sample").turns[0]["turn_id"]


def test_native_answer_actions_and_clipboard(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "scripts/verify_answer_actions.py",
            "--headless",
            "--output",
            str(tmp_path / "gui"),
        ],
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH="."),
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / "gui/result.json").read_text())
    assert len(report["checks"]) == 4 and report["model_sends"] == 0
