"""Tests for slash commands in the chat REPL."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from prompt_toolkit.styles import Style

from ultrakb.agent.chat import _handle_slash, _run_add, run_chat
from ultrakb.knowledge_scope import legacy_scope
from ultrakb.terminal_chat_state import TerminalConversation


def _setup_kb(tmp_path: Path) -> Path:
    from ultrakb.application.knowledge_bases import initialize_kb

    initialize_kb(tmp_path, seed_environment=False)
    return tmp_path


def _make_session(kb_dir: Path) -> TerminalConversation:
    return TerminalConversation.new(kb_dir, "gpt-4o-mini", "en")


def _record_turn(session, user, answer, *, attempt_id=None):
    from uuid import uuid4

    from ultrakb.storage.session_store import SessionStore

    store = SessionStore(session.kb_dir, session.scope.knowledge.identity)
    if not session.persisted:
        store.create(session.id, model=session.model, language=session.language)
    store.append_exchange(session.id, uuid4().hex, user, answer, attempt_id=attempt_id)
    session.reload()


_STYLE = Style.from_dict({})


def _collect_fmt():
    """Return (patch, collected) where collected is a list of printed strings."""
    collected: list[str] = []

    def _fake_fmt(_style, *fragments):
        for _cls, text in fragments:
            collected.append(text)

    return patch("ultrakb.agent.chat._fmt", _fake_fmt), collected


# --- /status and /list use click.echo, captured by capsys ---


@pytest.mark.asyncio
async def test_slash_status(tmp_path, capsys):
    kb_dir = _setup_kb(tmp_path)
    session = _make_session(kb_dir)
    result = await _handle_slash("/status", kb_dir, session, _STYLE)
    assert result is None
    output = capsys.readouterr().out
    assert "Knowledge Base Status" in output


@pytest.mark.asyncio
async def test_slash_list_empty(tmp_path, capsys):
    kb_dir = _setup_kb(tmp_path)
    session = _make_session(kb_dir)
    result = await _handle_slash("/list", kb_dir, session, _STYLE)
    assert result is None
    output = capsys.readouterr().out
    assert "No documents indexed yet" in output


@pytest.mark.asyncio
async def test_slash_list_with_docs(tmp_path, capsys):
    kb_dir = _setup_kb(tmp_path)
    from ultrakb.knowledge_scope import resolve_scope
    from ultrakb.storage.catalog_store import CatalogStore

    original = tmp_path / "paper.pdf"
    original.write_bytes(b"Retained fixture bytes")
    scope = resolve_scope(kb_dir)
    CatalogStore(kb_dir, scope.knowledge.identity).admit(original, source_key="paper-fixture")
    session = _make_session(kb_dir)
    result = await _handle_slash("/list", kb_dir, session, _STYLE)
    assert result is None
    output = capsys.readouterr().out
    assert "paper.pdf" in output


# --- /add, /exit, /clear, /help, /unknown use _fmt → need patching ---


@pytest.mark.asyncio
async def test_slash_add_missing_arg(tmp_path):
    kb_dir = _setup_kb(tmp_path)
    session = _make_session(kb_dir)
    p, collected = _collect_fmt()
    with p:
        result = await _handle_slash("/add", kb_dir, session, _STYLE)
    assert result is None
    assert any("Usage: /add <path>" in s for s in collected)


@pytest.mark.asyncio
async def test_slash_add_nonexistent_path(tmp_path):
    kb_dir = _setup_kb(tmp_path)
    session = _make_session(kb_dir)
    p, collected = _collect_fmt()
    with p:
        result = await _handle_slash("/add /no/such/path", kb_dir, session, _STYLE)
    assert result is None
    assert any("Path does not exist" in s for s in collected)


@pytest.mark.asyncio
async def test_slash_add_unsupported_type(tmp_path):
    kb_dir = _setup_kb(tmp_path)
    bad_file = tmp_path / "file.xyz"
    bad_file.write_text("data")
    session = _make_session(kb_dir)
    p, collected = _collect_fmt()
    with p:
        result = await _handle_slash(f"/add {bad_file}", kb_dir, session, _STYLE)
    assert result is None
    assert any("Unsupported file type" in s for s in collected)


@pytest.mark.asyncio
async def test_slash_add_single_file(tmp_path):
    kb_dir = _setup_kb(tmp_path)
    doc = tmp_path / "test.md"
    doc.write_text("# Hello")
    p, _collected = _collect_fmt()
    with p, patch("ultrakb.application.documents.add_single_file") as mock_add:
        await _run_add(str(doc), kb_dir, _STYLE)
        assert mock_add.call_args.args == (doc, kb_dir)
        selected = mock_add.call_args.kwargs["scope"]
        expected = legacy_scope(kb_dir)
        assert selected.publication_id == expected.publication_id
        assert selected.knowledge.identity == expected.knowledge.identity


@pytest.mark.asyncio
async def test_slash_add_directory_with_progress(tmp_path):
    kb_dir = _setup_kb(tmp_path)
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "a.md").write_text("# A")
    (docs_dir / "b.txt").write_text("B")
    (docs_dir / "skip.xyz").write_text("skip")
    p, collected = _collect_fmt()
    with p, patch("ultrakb.application.documents.add_single_file") as mock_add:
        await _run_add(str(docs_dir), kb_dir, _STYLE)
        assert mock_add.call_count == 2
        assert (
            mock_add.call_args_list[0].kwargs["context"]
            is mock_add.call_args_list[1].kwargs["context"]
        )
    output = "".join(collected)
    assert "Found 2 supported file(s)" in output
    assert "[1/2]" in output
    assert "[2/2]" in output


@pytest.mark.asyncio
async def test_slash_lint(tmp_path):
    kb_dir = _setup_kb(tmp_path)
    session = _make_session(kb_dir)
    with patch(
        "ultrakb.terminal_maintenance.run_lint",
        new_callable=AsyncMock,
        return_value=tmp_path / "report.md",
    ):
        result = await _handle_slash("/lint", kb_dir, session, _STYLE)
    assert result is None


@pytest.mark.asyncio
async def test_run_chat_handles_ctrl_c_during_slash_command(tmp_path):
    kb_dir = _setup_kb(tmp_path)
    session = _make_session(kb_dir)

    class _FakePromptSession:
        def __init__(self) -> None:
            self.calls = 0

        async def prompt_async(self) -> str:
            self.calls += 1
            if self.calls == 1:
                return "/lint"
            raise EOFError

    prompt = _FakePromptSession()
    p, collected = _collect_fmt()
    with (
        p,
        patch("ultrakb.agent.chat._print_header"),
        patch("ultrakb.agent.chat._make_prompt_session", return_value=prompt),
        patch(
            "ultrakb.agent.chat._handle_slash",
            new_callable=AsyncMock,
            side_effect=KeyboardInterrupt,
        ),
    ):
        await run_chat(kb_dir, session, no_color=True)

    assert prompt.calls == 2
    assert any("[aborted]" in s for s in collected)


@pytest.mark.asyncio
async def test_slash_unknown(tmp_path):
    kb_dir = _setup_kb(tmp_path)
    session = _make_session(kb_dir)
    p, collected = _collect_fmt()
    with p:
        result = await _handle_slash("/foobar", kb_dir, session, _STYLE)
    assert result is None
    assert any("Unknown command" in s for s in collected)


@pytest.mark.asyncio
async def test_slash_exit(tmp_path):
    kb_dir = _setup_kb(tmp_path)
    session = _make_session(kb_dir)
    p, _collected = _collect_fmt()
    with p:
        result = await _handle_slash("/exit", kb_dir, session, _STYLE)
    assert result == "exit"


@pytest.mark.asyncio
async def test_slash_clear(tmp_path):
    kb_dir = _setup_kb(tmp_path)
    session = _make_session(kb_dir)
    p, _collected = _collect_fmt()
    with p:
        result = await _handle_slash("/clear", kb_dir, session, _STYLE)
    assert result == "new_session"


@pytest.mark.asyncio
async def test_slash_save_preserves_previous_completed_history(tmp_path):
    kb_dir = _setup_kb(tmp_path)
    session = _make_session(kb_dir)
    _record_turn(session, "Question", "Completed answer")
    p, _collected = _collect_fmt()
    with p:
        await _handle_slash("/save named-copy", kb_dir, session, _STYLE)
        _record_turn(session, "Next question", "Next completed answer")
        await _handle_slash("/save named-copy", kb_dir, session, _STYLE)
    paths = list((kb_dir / "wiki/explorations").glob("named-copy-*.md"))
    assert len(paths) == 2
    assert all("Completed answer" in path.read_text() for path in paths)
    assert sum("Next completed answer" in path.read_text() for path in paths) == 1


@pytest.mark.asyncio
async def test_cancelled_slash_save_never_starts_after_its_lock_is_released(tmp_path):
    import asyncio
    import threading

    from ultrakb.locks import session_lock

    kb_dir = _setup_kb(tmp_path)
    session = _make_session(kb_dir)
    _record_turn(session, "Question", "Completed answer")
    ready, release = threading.Event(), threading.Event()

    def hold():
        with session_lock(kb_dir, session.id):
            ready.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert ready.wait(2)
        saving = asyncio.create_task(
            _handle_slash("/save cancelled-export", kb_dir, session, _STYLE)
        )
        await asyncio.sleep(0.15)
        saving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await saving
    finally:
        release.set()
        await asyncio.to_thread(holder.join, 2)
    await asyncio.sleep(0.15)
    assert not list((kb_dir / "wiki/explorations").glob("cancelled-export-*.md"))


def test_save_transcript_strips_ghost_wikilinks(tmp_path):
    """`/save` writes the chat transcript to wiki/explorations/. Assistant
    responses may contain [[wikilinks]] to pages that don't exist on disk
    (the agent's instructions encourage wikilinks but its view of which
    pages exist can drift). The save path strips them before writing so
    `ultrakb lint` doesn't surface them as broken links on the next run.
    """
    from ultrakb.agent.chat import _save_transcript

    kb_dir = _setup_kb(tmp_path)
    from ultrakb.desktop.verification_data import publish_pages

    publish_pages(kb_dir, {"concepts/attention.md": b"# Attention\n"})

    session = _make_session(kb_dir)
    _record_turn(
        session,
        "What is a transformer?",
        "A transformer uses [[concepts/attention]] to model relationships. "
        "Unlike [[concepts/rnn]], it processes tokens in parallel and relies "
        "on [[concepts/positional-encoding]] for order.",
    )

    result = _save_transcript(kb_dir, session, name=None)
    path = Path(result.resources[0])

    text = path.read_text(encoding="utf-8")
    # Valid link preserved
    assert "[[concepts/attention]]" in text
    # Ghost links stripped to plain text
    assert "[[concepts/rnn]]" not in text
    assert "rnn" in text
    assert "[[concepts/positional-encoding]]" not in text
    assert "positional encoding" in text
    # User turn preserved verbatim (not stripped)
    assert "What is a transformer?" in text
