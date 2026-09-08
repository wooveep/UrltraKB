"""Conversation outcomes through shared use cases and the external SDK seam."""

import asyncio

import pytest
from agents import RawResponsesStreamEvent, Runner
from openai.types.responses import ResponseTextDeltaEvent

from openkb.agent.chat_session import ChatSession, load_session
from openkb.application.conversations import continue_conversation
from openkb.application.execution import ExecutionContext


@pytest.mark.asyncio
async def test_concurrent_continuations_reload_completed_history(kb_dir, monkeypatch):
    first_running, finish_first, second_waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    session = ChatSession.new(kb_dir, "openai/original", "zh")
    session.record_turn("Original", "Saved", [])
    inputs = []

    class ModelRun:
        is_complete = False
        final_output = "Answer"

        def __init__(self, history):
            self.history = history
            inputs.append(history)

        async def stream_events(self):
            question = self.history[-1]["content"]
            if question == "First":
                first_running.set()
                await finish_first.wait()
            yield RawResponsesStreamEvent(
                data=ResponseTextDeltaEvent(
                    type="response.output_text.delta",
                    delta="Answer",
                    content_index=0,
                    item_id="message",
                    output_index=0,
                    sequence_number=0,
                    logprobs=[],
                )
            )
            self.is_complete = True

        def to_input_list(self):
            return self.history + [{"role": "assistant", "content": "Answer"}]

    monkeypatch.setattr(Runner, "run_streamed", lambda agent, history, **kw: ModelRun(history))
    first = asyncio.create_task(continue_conversation(kb_dir, "First", session_id=session.id))
    await asyncio.wait_for(first_running.wait(), 5)
    context = ExecutionContext(
        on_event=lambda event: second_waiting.set() if event.get("stage") == "waiting" else None
    )
    second = asyncio.create_task(
        continue_conversation(
            kb_dir,
            "Second",
            session_id=session.id,
            context=context,
        )
    )
    await asyncio.wait_for(second_waiting.wait(), 5)
    assert len(inputs) == 1
    finish_first.set()
    outcomes = await asyncio.wait_for(asyncio.gather(first, second), 5)
    assert [result.turn_count for result in outcomes] == [2, 3]
    assert [turn["content"] for turn in inputs[1] if turn["role"] == "user"] == ["First", "Second"]
    saved = load_session(kb_dir, session.id)
    assert saved.user_turns == ["Original", "First", "Second"]
    assert saved.model == "openai/original"
    assert saved.language == "zh"


@pytest.mark.asyncio
async def test_saved_answer_excludes_tool_narration_and_explicit_reasoning(kb_dir, monkeypatch):
    from openkb.application.conversations import read_conversation

    class ModelRun:
        is_complete = False
        final_output = (
            "<think>private deliberation</think>\n**可见回答**\n\nUse `<think>` literally."
        )

        async def stream_events(self):
            for index, text in enumerate(("I will inspect the sources. ", self.final_output)):
                yield RawResponsesStreamEvent(
                    data=ResponseTextDeltaEvent(
                        type="response.output_text.delta",
                        delta=text,
                        content_index=0,
                        item_id=f"message-{index}",
                        output_index=0,
                        sequence_number=index,
                        logprobs=[],
                    )
                )
            self.is_complete = True

        def to_input_list(self):
            return [{"role": "assistant", "content": self.final_output}]

    monkeypatch.setattr(Runner, "run_streamed", lambda *args, **kwargs: ModelRun())
    result = await continue_conversation(kb_dir, "请回答")
    assert result.status == "completed"
    assert result.answer == "**可见回答**\n\nUse `<think>` literally."
    saved = read_conversation(kb_dir, result.session_id)
    assert saved.turns == (("请回答", result.answer),)


@pytest.mark.asyncio
async def test_interrupted_submission_survives_reopen_without_a_completed_model_turn(
    kb_dir, monkeypatch
):
    from openkb.application.conversations import read_conversation

    stopped = False

    class ModelRun:
        is_complete = False
        final_output = "Answer"

        def cancel(self, **kwargs):
            self.is_complete = True

        async def stream_events(self):
            nonlocal stopped
            stopped = True
            yield RawResponsesStreamEvent(
                data=ResponseTextDeltaEvent(
                    type="response.output_text.delta",
                    delta="<think>unfinished private work",
                    content_index=0,
                    item_id="message",
                    output_index=0,
                    sequence_number=0,
                    logprobs=[],
                )
            )
            self.is_complete = True

        def to_input_list(self):
            return []

    monkeypatch.setattr(Runner, "run_streamed", lambda *args, **kwargs: ModelRun())
    result = await continue_conversation(
        kb_dir, "请保留这个问题", context=ExecutionContext(cancelled=lambda: stopped)
    )
    assert result.status == "stopped"
    restored = read_conversation(kb_dir, result.session_id)
    assert restored.turns == ()
    assert restored.timeline == (("请保留这个问题", "这次回答未完成，可以继续提问。"),)
    assert load_session(kb_dir, result.session_id).history == []

    stopped = False
    result = await continue_conversation(kb_dir, "接着问", session_id=result.session_id)
    assert result.status == "completed"
    restored = read_conversation(kb_dir, result.session_id)
    assert restored.turns == (("接着问", "Answer"),)
    assert len(restored.timeline) == 2


def test_queued_question_survives_restart_without_replaying_model_work(kb_dir, tmp_path):
    from openkb.application.conversations import read_conversation
    from openkb.desktop.chat_outbox import ChatOutbox
    from openkb.locks import kb_ingest_lock

    session = ChatSession.new(kb_dir, "", "")
    directory = tmp_path / "desktop-outbox"
    # Accepting a question must not wait for the long-running model's KB lease.
    with kb_ingest_lock(kb_dir / ".openkb"):
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
    from openkb.application.sessions import delete_conversation
    from openkb.desktop.chat_outbox import ChatOutbox

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

    from openkb.application.conversations import read_conversation
    from openkb.application.sessions import export_conversation
    from openkb.desktop.chat_outbox import ChatOutbox

    session = ChatSession.new(kb_dir, "openai/test", "zh")
    session.record_turn("原始问题", "原始回答", [])
    identities = iter(["f" * 32, "0" * 32])
    monkeypatch.setattr(
        "openkb.desktop.chat_outbox.uuid4", lambda: SimpleNamespace(hex=next(identities))
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

    from openkb.application.conversations import read_conversation
    from openkb.desktop.chat_outbox import ChatOutbox

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
    from openkb.application.conversations import read_conversation
    from openkb.desktop.chat_outbox import ChatOutbox

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
async def test_retry_recovered_submission_keeps_initial_model_binding(
    kb_dir, tmp_path, monkeypatch
):
    from openkb.config import resolve_effective_config
    from openkb.desktop.chat_outbox import ChatOutbox

    session = ChatSession.new(kb_dir, "", "")
    outbox = ChatOutbox(tmp_path / "outbox")
    accepted = outbox.accept(kb_dir, session.id, "重试排队的问题", new=True)
    outbox.recover(kb_dir)

    class ModelRun:
        is_complete = False
        final_output = "重试后的回答"

        async def stream_events(self):
            self.is_complete = True
            if False:
                yield

        def to_input_list(self):
            return [{"role": "assistant", "content": self.final_output}]

    monkeypatch.setattr(Runner, "run_streamed", lambda *args, **kwargs: ModelRun())
    result = await continue_conversation(
        kb_dir,
        "重试排队的问题",
        new_session_id=session.id,
        attempt_id=accepted.id,
        submission_order=accepted.order,
    )
    assert result.status == "completed"
    config = resolve_effective_config(kb_dir)[0]
    saved = load_session(kb_dir, session.id)
    assert (saved.model, saved.language) == (config["model"], config["language"])
    assert saved.incomplete == []
    assert saved.turn_count == 1
