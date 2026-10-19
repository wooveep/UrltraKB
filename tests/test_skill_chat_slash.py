"""Tests for the /skill new slash command inside ultrakb chat."""

from __future__ import annotations

import shlex

import pytest
from prompt_toolkit.styles import Style

from ultrakb.agent.chat import _handle_slash
from ultrakb.terminal_chat_state import TerminalConversation

pytest_plugins = ("test_vendor_sdk",)


def _make_kb(tmp_path):
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.desktop.verification_data import publish_pages

    initialize_kb(tmp_path, seed_environment=False)
    publish_pages(tmp_path, {"concepts/demo.md": b"# Demo\n", "summaries/demo.md": b"# Demo\n"})
    return tmp_path


@pytest.mark.asyncio
async def test_slash_skill_new_calls_generator(kb_dir, model_service):
    from short_generation import assert_formatter_delivery, retained_formatter

    kb = kb_dir
    intent = retained_formatter(kb, model_service.credentials)
    session = TerminalConversation.new(kb, "deepseek/deepseek-flash", "en")
    style = Style.from_dict({})

    action = await _handle_slash("/skill new demo " + shlex.quote(intent), kb, session, style)

    assert action is None  # continues chat session
    assert_formatter_delivery(kb, "demo", model_service)
    assert (kb / ".claude-plugin" / "marketplace.json").exists()


@pytest.mark.asyncio
async def test_slash_skill_new_reports_usage_when_args_missing(tmp_path):
    kb = _make_kb(tmp_path)
    session = TerminalConversation.new(kb, "gpt-4o-mini", "en")
    style = Style.from_dict({})

    action = await _handle_slash("/skill new", kb, session, style)
    assert action is None
    # No skill written
    assert not (kb / "output").exists()


@pytest.mark.asyncio
async def test_slash_skill_unknown_subcommand(tmp_path):
    kb = _make_kb(tmp_path)
    session = TerminalConversation.new(kb, "gpt-4o-mini", "en")
    style = Style.from_dict({})
    action = await _handle_slash("/skill list", kb, session, style)
    assert action is None


@pytest.mark.asyncio
async def test_slash_skill_new_rejects_empty_wiki(tmp_path):
    """Chat / slash command must catch freshly-init'd KBs (no compiled content)."""
    from ultrakb.application.knowledge_bases import initialize_kb

    kb = tmp_path
    initialize_kb(kb, seed_environment=False)

    session = TerminalConversation.new(kb, "gpt-4o-mini", "en")
    style = Style.from_dict({})

    action = await _handle_slash('/skill new demo "intent"', kb, session, style)
    assert action is None
    assert not (kb / "output").exists()


def test_preflight_gate_counts_entities(tmp_path):
    """The wiki-content gate must accept a KB whose only compiled content
    lives in entities/ (no concept or summary pages yet)."""
    from ultrakb.application.generators import preflight_generation
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.desktop.verification_data import publish_pages

    initialize_kb(tmp_path, seed_environment=False)
    publish_pages(tmp_path, {"entities/ada.md": b"# Ada\n"})
    assert preflight_generation(tmp_path, "demo") is None


@pytest.mark.asyncio
async def test_slash_skill_new_rejects_when_target_exists(tmp_path):
    """Chat / slash command must not silently overwrite an existing skill."""
    kb = _make_kb(tmp_path)
    (kb / "output" / "skills" / "demo").mkdir(parents=True)
    (kb / "output" / "skills" / "demo" / "stale.txt").write_text("old")

    session = TerminalConversation.new(kb, "gpt-4o-mini", "en")
    style = Style.from_dict({})

    action = await _handle_slash('/skill new demo "intent"', kb, session, style)
    assert action is None
    # stale.txt must still be there (we didn't overwrite)
    assert (kb / "output" / "skills" / "demo" / "stale.txt").read_text() == "old"
