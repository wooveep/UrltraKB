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
    from ultrakb.agent.chat_session import ChatSession, load_session
    from ultrakb.application.conversations import read_conversation

    session = ChatSession.new(kb_dir, "stored-ui-sample", "zh")
    first = {"input_cached": 100, "input_uncached": 20, "output": 10, "reasoning": 2}
    second = {"input_cached": 0, "input_uncached": 5, "output": 3, "reasoning": None}
    session.record_turn("first", "# 第一条\n\n[[summaries/policy]]", [], usage=first)
    session.record_turn("second", "第二条", [], usage=second)
    session.record_usage(
        {"input_cached": 5, "input_uncached": 6, "output": 7, "reasoning": 0},
        baseline=session.token_usage,
    )
    session.record_turn("incremental", "已逐次记账的第三条", [], usage=None, turn_usage=second)
    session.begin_attempt("尚未完成")
    view = read_conversation(kb_dir, session.id)
    assert view.answers[0].usage == first and view.answers[1].usage == second
    assert view.answers[2].usage == second
    assert view.timeline_answers[-1] is None
    assert view.answers[0].markdown.endswith("[[summaries/policy]]")
    assert view.answers[0].identity == load_session(kb_dir, session.id).turns[0].turn_id
    # Existing persisted v2 exchanges can omit the optional per-turn receipt.
    raw = json.loads(session.path.read_text())
    for turn in raw["turns"]:
        turn.pop("usage", None)
    from ultrakb.locks import atomic_write_json, kb_ingest_lock

    with kb_ingest_lock(kb_dir / ".ultrakb"):
        atomic_write_json(session.path, raw)
    restored = read_conversation(kb_dir, session.id)
    assert all(answer.usage is None for answer in restored.answers)
    assert restored.usage is not None


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
