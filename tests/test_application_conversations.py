"""Conversation outcomes through shared use cases and the external SDK seam."""

import asyncio

import pytest
from agents import Runner
from application_llm_fixtures import real_application_model  # noqa: F401

from ultrakb.agent.chat_session import ChatSession, load_session
from ultrakb.application.conversations import continue_conversation
from ultrakb.application.execution import ExecutionContext


@pytest.fixture(autouse=True)
def original_for_conversation(kb_dir):
    from ultrakb.config import load_config, save_config
    from ultrakb.locks import atomic_write_text

    atomic_write_text(kb_dir / "wiki/sources/fixture.md", "The recorded answer is 42.")
    path = kb_dir / ".ultrakb/config.yaml"
    config = load_config(path)
    config.update(model="deepseek/deepseek-flash", language="en")
    config["role_generation"] = {"answer": {"max_tokens": 256, "reasoning_effort": "none"}}
    save_config(path, config)


@pytest.mark.asyncio
async def test_concurrent_continuations_reload_completed_history(kb_dir, monkeypatch):
    first_running, finish_first, second_waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    session = ChatSession.new(kb_dir, "deepseek/deepseek-flash", "en")
    session.record_turn("Original", "Saved", [])
    inputs = []
    genuine_run = Runner.run_streamed

    def observe(agent, history, **options):
        inputs.append(history)
        delivered = genuine_run(agent, history, **options)
        genuine_events = delivered.stream_events

        async def events():
            question = history[-1]["content"][-1]["text"]
            if question == "First":
                first_running.set()
                await finish_first.wait()
            async for event in genuine_events():
                yield event

        delivered.stream_events = events
        return delivered

    monkeypatch.setattr(Runner, "run_streamed", observe)
    first = asyncio.create_task(continue_conversation(kb_dir, "First", session_id=session.id))
    await asyncio.wait_for(first_running.wait(), 30)
    context = ExecutionContext(
        on_event=lambda event: second_waiting.set() if event.get("stage") == "waiting" else None
    )
    second = asyncio.create_task(
        continue_conversation(kb_dir, "Second", session_id=session.id, context=context)
    )
    await asyncio.wait_for(second_waiting.wait(), 30)
    assert len(inputs) == 1
    finish_first.set()
    outcomes = await asyncio.wait_for(asyncio.gather(first, second), 90)
    assert [result.turn_count for result in outcomes] == [2, 3]
    assert len(inputs[1]) == 1 and inputs[1][0]["role"] == "user"
    content = inputs[1][0]["content"]
    assert content[-1]["text"] == "Second"
    assert '"text": "First"' in content[0]["text"]
    assert "Saved" not in content[0]["text"]
    saved = load_session(kb_dir, session.id)
    assert saved.user_turns == ["Original", "First", "Second"]
    assert saved.model == "deepseek/deepseek-flash"
    assert saved.language == "en"


@pytest.mark.asyncio
async def test_saved_answer_excludes_tool_narration_and_explicit_reasoning(kb_dir, monkeypatch):
    from markdown_it import MarkdownIt

    from ultrakb.agent.answer_text import visible_answer
    from ultrakb.application.conversations import read_conversation
    from ultrakb.locks import atomic_write_text

    # Literal syntax and explicit reasoning are parser inputs, not replacement replies.
    raw = "<think>private deliberation</think>\n**可见回答**\n\nUse `<think>` literally."
    assert visible_answer(raw) == "**可见回答**\n\nUse `<think>` literally."
    atomic_write_text(kb_dir / "wiki/sources/fixture.md", "Use `<think>` literally.")
    actual = []
    genuine_run = Runner.run_streamed

    def observe(*args, **options):
        delivered = genuine_run(*args, **options)
        actual.append(delivered)
        return delivered

    monkeypatch.setattr(Runner, "run_streamed", observe)
    question = (
        "Read sources/fixture.md once and quote its sentence exactly beneath "
        "the heading **可见回答**."
    )
    result = await continue_conversation(kb_dir, question)
    assert result.status == "completed", result.error
    visible_blocks = [
        "".join(child.content for child in token.children or [] if child.type == "text")
        for token in MarkdownIt().parse(result.answer)
        if token.type == "inline"
    ]
    assert "可见回答" in visible_blocks
    assert "Use `<think>` literally." in result.answer
    assert "I will inspect" not in result.answer and "private deliberation" not in result.answer
    assert result.answer == visible_answer(actual[-1].final_output)
    saved = read_conversation(kb_dir, result.session_id)
    assert saved.turns == ((question, result.answer),)


@pytest.mark.asyncio
async def test_interrupted_submission_survives_reopen_without_a_completed_model_turn(kb_dir):
    from ultrakb.application.conversations import read_conversation

    stopped = False

    def cancel_after_real_delta(event):
        nonlocal stopped
        if event.get("event") == "delta":
            stopped = True

    result = await continue_conversation(
        kb_dir,
        "请保留这个问题",
        context=ExecutionContext(cancelled=lambda: stopped, on_event=cancel_after_real_delta),
    )
    assert result.status == "stopped"
    restored = read_conversation(kb_dir, result.session_id)
    assert restored.turns == ()
    assert restored.timeline == (("请保留这个问题", "这次回答未完成，可以继续提问。"),)
    assert load_session(kb_dir, result.session_id).history == []

    stopped = False
    result = await continue_conversation(kb_dir, "接着问", session_id=result.session_id)
    assert result.status == "completed", result.error
    restored = read_conversation(kb_dir, result.session_id)
    assert result.answer_outcome == "answered"
    assert restored.turns == (("接着问", result.answer),)
    assert len(restored.timeline) == 2


def test_queued_question_survives_restart_without_replaying_model_work(kb_dir, tmp_path):
    from ultrakb.application.conversations import read_conversation
    from ultrakb.desktop.chat_outbox import ChatOutbox
    from ultrakb.locks import kb_ingest_lock

    session = ChatSession.new(kb_dir, "", "")
    directory = tmp_path / "desktop-outbox"
    # Accepting a question must not wait for the long-running model's KB lease.
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        ChatOutbox(directory).accept(kb_dir, session.id, "排队后被停止的问题", new=True)
        assert not session.path.exists()
    restarted = ChatOutbox(directory)
    restarted.recover(kb_dir)
    restarted.recover(kb_dir)
    saved = read_conversation(kb_dir, session.id)
    assert saved.turns == ()
    assert saved.timeline == (("排队后被停止的问题", "这次回答未完成，可以继续提问。"),)
    assert load_session(kb_dir, session.id).history == []


@pytest.mark.parametrize("new", [False, True])
def test_outbox_recovery_deduplicates_committed_answer_and_respects_deletion(kb_dir, tmp_path, new):
    from ultrakb.application.sessions import delete_conversation
    from ultrakb.desktop.chat_outbox import ChatOutbox

    session = ChatSession.new(kb_dir, "openai/test", "zh")
    outbox = ChatOutbox(tmp_path / "outbox")
    attempt = outbox.accept(kb_dir, session.id, "已回答的问题", new=True)
    session.begin_attempt("已回答的问题", identity=attempt.id, submission_order=attempt.order)
    session.record_turn("已回答的问题", "已确认的回答", [], attempt_id=attempt.id)
    # Simulate a crash after the KB commit but before desktop acknowledgement.
    outbox.recover(kb_dir)
    saved = load_session(kb_dir, session.id)
    assert saved.incomplete == []
    assert saved.user_turns == ["已回答的问题"]
    outbox.accept(kb_dir, session.id, "等待中的问题", new=new)
    delete_conversation(kb_dir, session.id, version=saved._version)
    outbox.recover(kb_dir)
    assert not session.path.exists()


def test_recovered_submissions_keep_acceptance_order_and_original_turn_position(
    kb_dir, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from ultrakb.application.conversations import read_conversation
    from ultrakb.application.sessions import export_conversation
    from ultrakb.desktop.chat_outbox import ChatOutbox

    session = ChatSession.new(kb_dir, "openai/test", "zh")
    session.record_turn("原始问题", "原始回答", [])
    identities = iter(["f" * 32, "0" * 32])
    monkeypatch.setattr(
        "ultrakb.desktop.chat_outbox.uuid4", lambda: SimpleNamespace(hex=next(identities))
    )
    outbox = ChatOutbox(tmp_path / "outbox")
    outbox.accept(kb_dir, session.id, "先提交", new=False, after_turn=1)
    outbox.accept(kb_dir, session.id, "后提交", new=False, after_turn=1)
    session.record_turn("后来的完整问题", "后来的回答", [])
    outbox.recover(kb_dir)
    assert [q for q, _ in read_conversation(kb_dir, session.id).timeline] == [
        "原始问题",
        "先提交",
        "后提交",
        "后来的完整问题",
    ]
    from pathlib import Path

    transcript = Path(export_conversation(kb_dir, session.id).resources[0]).read_text()
    assert (
        transcript.index("先提交") < transcript.index("后提交") < transcript.index("后来的完整问题")
    )


def test_old_kb_submissions_do_not_block_recreated_kb_history(kb_dir, tmp_path, tmp_path_factory):
    import shutil

    from ultrakb.application.conversations import read_conversation
    from ultrakb.desktop.chat_outbox import ChatOutbox

    old = ChatSession.new(kb_dir, "", "")
    outbox = ChatOutbox(tmp_path_factory.mktemp("chat-settings"))
    outbox.accept(kb_dir, old.id, "旧知识库问题", new=True)
    retired = tmp_path.with_name(tmp_path.name + "-retired")
    kb_dir.rename(retired)
    shutil.copytree(retired, kb_dir)
    current = ChatSession.new(kb_dir, "", "")
    outbox.accept(kb_dir, current.id, "新知识库问题", new=True)
    outbox.recover(kb_dir)
    outbox.recover(kb_dir)
    assert not old.path.exists()
    assert read_conversation(kb_dir, current.id).timeline[0][0] == "新知识库问题"


def test_deferred_recovery_orders_question_before_already_started_later_attempt(kb_dir, tmp_path):
    from ultrakb.application.conversations import read_conversation
    from ultrakb.desktop.chat_outbox import ChatOutbox

    session = ChatSession.new(kb_dir, "openai/test", "zh")
    session.record_turn("之前的问题", "之前的回答", [])
    outbox = ChatOutbox(tmp_path / "outbox")
    outbox.accept(kb_dir, session.id, "先提交后恢复", new=False, after_turn=1)
    later = outbox.accept(kb_dir, session.id, "后提交先执行", new=False, after_turn=1)
    session.begin_attempt("后提交先执行", identity=later.id, submission_order=later.order)
    outbox.discard(kb_dir, later.id)
    outbox.recover(kb_dir)
    assert [q for q, _ in read_conversation(kb_dir, session.id).timeline] == [
        "之前的问题",
        "先提交后恢复",
        "后提交先执行",
    ]


@pytest.mark.asyncio
async def test_retry_recovered_submission_keeps_initial_model_binding(kb_dir, tmp_path):
    from ultrakb.config import resolve_effective_config
    from ultrakb.desktop.chat_outbox import ChatOutbox

    session = ChatSession.new(kb_dir, "", "")
    outbox = ChatOutbox(tmp_path / "outbox")
    accepted = outbox.accept(kb_dir, session.id, "重试排队的问题", new=True)
    outbox.recover(kb_dir)
    result = await continue_conversation(
        kb_dir,
        "重试排队的问题",
        new_session_id=session.id,
        attempt_id=accepted.id,
        submission_order=accepted.order,
    )
    assert result.status == "completed", result.error
    config = resolve_effective_config(kb_dir)[0]
    saved = load_session(kb_dir, session.id)
    assert (saved.model, saved.language) == (config["model"], config["language"])
    assert saved.incomplete == []
    assert saved.turn_count == 1
