"""Tests for the /skill new slash command inside ultrakb chat."""

from __future__ import annotations

import shlex

import pytest
from prompt_toolkit.styles import Style

from ultrakb.agent.chat import _handle_slash
from ultrakb.agent.chat_session import ChatSession

pytest_plugins = ("test_vendor_sdk",)


def _make_kb(tmp_path):
    (tmp_path / ".ultrakb").mkdir()
    (tmp_path / ".ultrakb" / "config.yaml").write_text("model: gpt-4o-mini\n")
    from kb_fixtures import mark_current_kb

    mark_current_kb(tmp_path)
    (tmp_path / ".ultrakb" / "chats").mkdir()
    (tmp_path / "wiki" / "concepts").mkdir(parents=True)
    (tmp_path / "wiki" / "summaries").mkdir(parents=True)
    (tmp_path / "wiki" / "index.md").write_text("# index\n")
    # Populate so wiki-content gate accepts
    (tmp_path / "wiki" / "concepts" / "demo.md").write_text("# demo\n")
    (tmp_path / "wiki" / "summaries" / "demo.md").write_text("# demo\n")
    return tmp_path


@pytest.mark.asyncio
async def test_slash_skill_new_calls_generator(kb_dir, model_service):
    from short_generation import assert_formatter_delivery, retained_formatter

    kb = kb_dir
    intent = retained_formatter(kb, model_service.credentials)
    session = ChatSession.new(kb, "deepseek/deepseek-flash", "en")
    style = Style.from_dict({})

    action = await _handle_slash("/skill new demo " + shlex.quote(intent), kb, session, style)

    assert action is None  # continues chat session
    assert_formatter_delivery(kb, "demo", model_service)
    assert (kb / ".claude-plugin" / "marketplace.json").exists()


@pytest.mark.asyncio
async def test_slash_skill_new_reports_usage_when_args_missing(tmp_path):
    kb = _make_kb(tmp_path)
    session = ChatSession.new(kb, "gpt-4o-mini", "en")
    style = Style.from_dict({})

    action = await _handle_slash("/skill new", kb, session, style)
    assert action is None
    # No skill written
    assert not (kb / "output").exists()


@pytest.mark.asyncio
async def test_slash_skill_unknown_subcommand(tmp_path):
    kb = _make_kb(tmp_path)
    session = ChatSession.new(kb, "gpt-4o-mini", "en")
    style = Style.from_dict({})
    action = await _handle_slash("/skill list", kb, session, style)
    assert action is None


@pytest.mark.asyncio
async def test_slash_skill_new_rejects_empty_wiki(tmp_path):
    """Chat / slash command must catch freshly-init'd KBs (no compiled content)."""
    kb = tmp_path
    (kb / ".ultrakb").mkdir()
    (kb / ".ultrakb" / "config.yaml").write_text("model: gpt-4o-mini\n")
    from kb_fixtures import mark_current_kb

    mark_current_kb(kb)
    (kb / ".ultrakb" / "chats").mkdir()
    # Empty wiki/ — exactly what `ultrakb init` creates
    (kb / "wiki" / "concepts").mkdir(parents=True)
    (kb / "wiki" / "summaries").mkdir(parents=True)
    (kb / "wiki" / "index.md").write_text("# index\n")

    session = ChatSession.new(kb, "gpt-4o-mini", "en")
    style = Style.from_dict({})

    action = await _handle_slash('/skill new demo "intent"', kb, session, style)
    assert action is None
    assert not (kb / "output").exists()


def test_preflight_gate_counts_entities(tmp_path):
    """The wiki-content gate must accept a KB whose only compiled content
    lives in entities/ (no concept or summary pages yet)."""
    from ultrakb.cli import _preflight_skill_new

    kb = tmp_path
    (kb / "wiki" / "entities").mkdir(parents=True)
    (kb / "wiki" / "entities" / "ada.md").write_text("# Ada\n")

    # No error means the gate passed.
    assert _preflight_skill_new(kb, "demo") is None


@pytest.mark.asyncio
async def test_slash_skill_new_rejects_when_target_exists(tmp_path):
    """Chat / slash command must not silently overwrite an existing skill."""
    kb = _make_kb(tmp_path)
    (kb / "wiki" / "concepts" / "x.md").write_text("x")
    (kb / "wiki" / "summaries" / "x.md").write_text("x")
    (kb / "output" / "skills" / "demo").mkdir(parents=True)
    (kb / "output" / "skills" / "demo" / "stale.txt").write_text("old")

    session = ChatSession.new(kb, "gpt-4o-mini", "en")
    style = Style.from_dict({})

    action = await _handle_slash('/skill new demo "intent"', kb, session, style)
    assert action is None
    # stale.txt must still be there (we didn't overwrite)
    assert (kb / "output" / "skills" / "demo" / "stale.txt").read_text() == "old"
