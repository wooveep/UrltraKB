"""Terminal history and REPL actions use retained main-store conversations."""

import asyncio
from threading import Event, Thread

import pytest
from click.testing import CliRunner
from stream_transport import StreamTransport
from test_storage_questions import question_knowledge

from ultrakb.application.conversations import read_conversation
from ultrakb.application.sessions import list_conversations
from ultrakb.application.views import storage_scope
from ultrakb.storage.session_store import SessionStore


def test_terminal_resume_reads_only_the_latest_display_window(tmp_path, capsys):
    from ultrakb.agent.chat import _build_style, _print_resume_view
    from ultrakb.terminal_chat_state import TerminalConversation

    root, identity, _ = question_knowledge(tmp_path)
    store = SessionStore(root, identity)
    store.create("long-terminal", model="gpt-4o-mini", language="en")
    for ordinal in range(70):
        store.append_exchange("long-terminal", f"turn-{ordinal}", f"Q{ordinal}", f"A{ordinal}")
    session = TerminalConversation.resume(
        root, "long-terminal", scope=storage_scope(root, identity)
    )
    assert session.turn_count == 70
    assert session.user_turns == [f"Q{i}" for i in range(65, 70)]
    _print_resume_view(session, _build_style(False))
    output = capsys.readouterr().out
    assert "65 earlier turn(s) omitted" in output and "[66]" in output
    assert "Resumed session  70 turn(s)" in output


def test_terminal_repl_persists_answers_exports_and_starts_a_fresh_session(tmp_path, monkeypatch):
    from ultrakb.agent import chat
    from ultrakb.terminal_chat_state import TerminalConversation

    root, identity, _ = question_knowledge(tmp_path)
    selected = storage_scope(root, identity)
    session = TerminalConversation.new(root, "gpt-4o-mini", "en", scope=selected)
    assert list_conversations(root, scope=selected) == []
    inputs = iter(
        ["First question.", "/save original", "/save original", "/clear", "Second question."]
    )

    class Prompt:
        async def prompt_async(self):
            try:
                return next(inputs)
            except StopIteration:
                raise EOFError from None

    monkeypatch.setattr(chat, "_make_prompt_session", lambda *args: Prompt())
    with StreamTransport([{"content": "First answer."}, {"content": "Second answer."}]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        asyncio.run(chat.run_chat(root, session, no_color=True, raw=True, scope=selected))
    assert len(service.requests) == 2
    assert session.turn_count == 1 and session.assistant_texts == ["First answer."]
    sessions = list_conversations(root, scope=storage_scope(root, identity))
    assert len(sessions) == 2 and all(item["turn_count"] == 1 for item in sessions)
    bound = storage_scope(root, identity).knowledge
    exports = [path for path in bound.files() if path.startswith("explorations/")]
    assert len(exports) == 2 and all(b"First answer." in bound.read(path) for path in exports)
    assert not (root / ".ultrakb/chats").exists()
    assert read_conversation(root, session.id, scope=storage_scope(root, identity)).turns == (
        ("First question.", "First answer."),
    )


def test_cli_resume_and_list_use_the_selected_main_store(tmp_path, monkeypatch):
    from ultrakb import cli as commands
    from ultrakb.agent import chat
    from ultrakb.terminal_chat_state import TerminalConversation

    root, identity, _ = question_knowledge(tmp_path)
    selected = storage_scope(root, identity)
    store = SessionStore(root, identity)
    store.create("cli-history", model="gpt-4o-mini", language="en")
    store.append_exchange("cli-history", "turn", "Question.", "Original answer.")
    monkeypatch.setattr(commands, "_selected_scope", lambda *args: selected)
    opened = []

    async def show(root, session, **options):
        assert isinstance(session, TerminalConversation)
        opened.append(session)

    monkeypatch.setattr(chat, "run_chat", show)
    runner = CliRunner()
    resumed = runner.invoke(commands.cli, ["--kb-dir", str(root), "chat", "--resume", "cli-"])
    assert resumed.exit_code == 0 and opened, resumed.output
    assert opened[0].assistant_texts == ["Original answer."]
    listed = runner.invoke(commands.cli, ["--kb-dir", str(root), "chat", "--list"])
    assert listed.exit_code == 0 and "cli-history" in listed.output, listed.output
    assert ".ultrakb/chats" not in listed.output


@pytest.mark.asyncio
async def test_cancelled_terminal_export_cannot_publish_after_the_session_is_unlocked(tmp_path):
    from ultrakb.agent.chat import _build_style, _handle_slash
    from ultrakb.locks import session_lock
    from ultrakb.terminal_chat_state import TerminalConversation

    root, identity, initial = question_knowledge(tmp_path)
    store = SessionStore(root, identity)
    store.create("cancel-export", model="gpt-4o-mini", language="en")
    store.append_exchange("cancel-export", "turn", "Question.", "Answer.")
    selected = storage_scope(root, identity)
    session = TerminalConversation.resume(root, "cancel-export", scope=selected)
    ready, release = Event(), Event()

    def hold():
        with session_lock(root, session.id, database_identity=identity):
            ready.set()
            assert release.wait(5)

    holder = Thread(target=hold)
    holder.start()
    saving = None
    try:
        assert await asyncio.to_thread(ready.wait, 2)
        saving = asyncio.create_task(
            _handle_slash("/save cancelled", root, session, _build_style(False), scope=selected)
        )
        await asyncio.sleep(0.1)
        saving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(saving, 2)
    finally:
        release.set()
        await asyncio.to_thread(holder.join, 2)
        if saving is not None and not saving.done():
            saving.cancel()
            await asyncio.gather(saving, return_exceptions=True)
    assert storage_scope(root, identity).publication_id == initial.publication_id


def test_terminal_export_reports_a_paused_projection(tmp_path, monkeypatch, capsys):
    from ultrakb.agent import chat
    from ultrakb.application.wiki_worktree import begin_external_edit
    from ultrakb.storage.wiki_projection import ProjectionStore
    from ultrakb.terminal_chat_state import TerminalConversation

    root, identity, _ = question_knowledge(tmp_path)
    store = SessionStore(root, identity)
    store.create("paused-terminal", model="gpt-4o-mini", language="en")
    store.append_exchange("paused-terminal", "turn", "Question.", "Original answer.")
    ProjectionStore(root, identity).sync()
    begin_external_edit(root, identity)
    selected = storage_scope(root, identity)
    session = TerminalConversation.resume(root, "paused-terminal", scope=selected)
    result = asyncio.run(
        chat._handle_slash("/save", root, session, chat._build_style(False), scope=selected)
    )
    assert result is None
    output = capsys.readouterr().out
    assert "Publication:" in output and "Wiki synchronization: paused" in output
    assert list((root / "wiki/explorations").glob("*.md")) == []
    assert any(
        path.startswith("explorations/") for path in storage_scope(root, identity).knowledge.files()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/list", "/status", "/save", "/help", "/exit"])
async def test_terminal_commands_remain_cancellable_while_reclamation_is_held(tmp_path, command):
    from ultrakb.agent.chat import _build_style, _handle_slash
    from ultrakb.storage.coordination import try_reclamation
    from ultrakb.terminal_chat_state import TerminalConversation

    root, identity, _ = question_knowledge(tmp_path)
    store = SessionStore(root, identity)
    store.create("busy-terminal", model="gpt-4o-mini", language="en")
    store.append_exchange("busy-terminal", "turn", "Question.", "Answer.")
    selected = storage_scope(root, identity)
    session = TerminalConversation.resume(root, "busy-terminal", scope=selected)
    with try_reclamation(root, identity) as held:
        assert held is not None
        command_task = asyncio.create_task(
            _handle_slash(command, root, session, _build_style(False), scope=selected)
        )
        if command in {"/help", "/exit"}:
            await asyncio.wait_for(command_task, 1)
        else:
            await asyncio.sleep(0.1)
            assert not command_task.done()
            command_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(command_task, 1)
