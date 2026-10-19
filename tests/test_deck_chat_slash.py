"""Tests for the /deck new slash command inside ultrakb chat."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from prompt_toolkit.styles import Style

from ultrakb.agent.chat import _handle_slash
from ultrakb.terminal_chat_state import TerminalConversation


def _make_kb(tmp_path):
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.desktop.verification_data import publish_pages

    initialize_kb(tmp_path, seed_environment=False)
    publish_pages(tmp_path, {"concepts/demo.md": b"# Demo\n", "summaries/demo.md": b"# Demo\n"})
    return tmp_path


@pytest.mark.asyncio
@pytest.mark.parametrize("critique", [False, True])
async def test_slash_deck_new_invokes_the_shared_generator(tmp_path, critique):
    from types import SimpleNamespace

    kb = _make_kb(tmp_path)
    session = TerminalConversation.new(kb, "gpt-4o-mini", "en")
    style = Style.from_dict({})
    result = SimpleNamespace(status="completed", validation=None)
    with patch(
        "ultrakb.application.generators.generate_artifact",
        new_callable=AsyncMock,
        return_value=result,
    ) as generate:
        flag = "--critique " if critique else ""
        action = await _handle_slash(f'/deck new {flag}demo "test intent"', kb, session, style)
    assert action is None
    generate.assert_awaited_once()
    called_root, options = generate.call_args.args
    assert called_root == kb
    assert options.target_type == "deck" and options.name == "demo"
    assert options.intent == "test intent" and options.critique is critique


@pytest.mark.asyncio
async def test_slash_deck_new_reports_usage_when_args_missing(tmp_path):
    kb = _make_kb(tmp_path)
    session = TerminalConversation.new(kb, "gpt-4o-mini", "en")
    style = Style.from_dict({})

    action = await _handle_slash("/deck new", kb, session, style)
    assert action is None
    # No deck written
    assert not (kb / "output").exists()


@pytest.mark.asyncio
async def test_slash_deck_unknown_subcommand(tmp_path):
    kb = _make_kb(tmp_path)
    session = TerminalConversation.new(kb, "gpt-4o-mini", "en")
    style = Style.from_dict({})
    action = await _handle_slash("/deck list", kb, session, style)
    assert action is None


@pytest.mark.asyncio
async def test_slash_deck_new_rejects_empty_wiki(tmp_path):
    """Chat / slash command must catch freshly-init'd KBs (no compiled content)."""
    from ultrakb.application.knowledge_bases import initialize_kb

    kb = tmp_path
    initialize_kb(kb, seed_environment=False)

    session = TerminalConversation.new(kb, "gpt-4o-mini", "en")
    style = Style.from_dict({})

    action = await _handle_slash('/deck new demo "intent"', kb, session, style)
    assert action is None
    assert not (kb / "output").exists()


@pytest.mark.asyncio
async def test_slash_deck_new_rejects_when_target_exists(tmp_path):
    """Chat / slash command must not silently overwrite an existing deck."""
    kb = _make_kb(tmp_path)
    (kb / "output" / "decks" / "demo").mkdir(parents=True)
    (kb / "output" / "decks" / "demo" / "stale.html").write_text("old")

    session = TerminalConversation.new(kb, "gpt-4o-mini", "en")
    style = Style.from_dict({})

    action = await _handle_slash('/deck new demo "intent"', kb, session, style)
    assert action is None
    # stale.html must still be there (we didn't overwrite)
    assert (kb / "output" / "decks" / "demo" / "stale.html").read_text() == "old"
