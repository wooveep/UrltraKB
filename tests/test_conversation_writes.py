"""Completed files and completed turns have independent recovery boundaries."""

import asyncio
import os
import subprocess
import sys

import pytest
from agents import RawResponsesStreamEvent, Runner
from openai.types.responses import ResponseTextDeltaEvent

from openkb.agent.chat_session import ChatSession, load_session
from openkb.agent.tools import write_kb_file
from openkb.application.conversations import ask_question, continue_conversation
from openkb.application.execution import ExecutionContext
from openkb.locks import kb_read_lock


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["complete", "stop", "error"])
@pytest.mark.parametrize("dispatch", ["task", "thread"])
async def test_chat_keeps_completed_tool_files_without_inventing_a_turn(
    kb_dir, monkeypatch, ending, dispatch
):
    session = ChatSession.new(kb_dir, "openai/test", "en")
    session.record_turn("old", "old answer", [])
    stopped = False

    class ModelRun:
        is_complete = False
        final_output = "new answer"

        async def stream_events(self):
            nonlocal stopped

            async def sdk_tool():
                assert write_kb_file("output/report.html", "<p>Saved</p>", str(kb_dir)).startswith(
                    "Written:"
                )

            if dispatch == "task":
                await asyncio.create_task(sdk_tool())
            else:
                await asyncio.to_thread(
                    write_kb_file, "output/report.html", "<p>Saved</p>", str(kb_dir)
                )
            stopped = ending == "stop"
            if ending == "error":
                raise RuntimeError("provider failed")
            yield RawResponsesStreamEvent(
                data=ResponseTextDeltaEvent(
                    type="response.output_text.delta",
                    delta="new answer",
                    content_index=0,
                    item_id="message",
                    output_index=0,
                    sequence_number=0,
                    logprobs=[],
                )
            )
            self.is_complete = True

        def to_input_list(self):
            return [{"role": "assistant", "content": "new answer"}]

        def cancel(self, **kwargs):
            self.is_complete = True

    monkeypatch.setattr(Runner, "run_streamed", lambda *args, **kw: ModelRun())
    result = await continue_conversation(
        kb_dir,
        "new",
        session_id=session.id,
        context=ExecutionContext(cancelled=lambda: stopped),
    )
    assert result.status == {"complete": "completed", "stop": "stopped", "error": "failed"}[ending]
    assert str(kb_dir / "output/report.html") in result.resources
    assert "created: output/report.html" in result.changes
    assert load_session(kb_dir, session.id).turn_count == (2 if ending == "complete" else 1)
    assert (kb_dir / "output/report.html").read_text() == "<p>Saved</p>"
    assert not list((kb_dir / ".openkb/journal").glob("*.json"))


@pytest.mark.parametrize("during_write", [False, True])
def test_crash_recovers_only_uncommitted_tool_write(kb_dir, during_write):
    artifact = kb_dir / "output/report.html"
    artifact.parent.mkdir(exist_ok=True)
    artifact.write_text("original")
    script = """
import os, sys
from pathlib import Path
from openkb.locks import kb_ingest_lock
from openkb.model_outputs import model_output_scope
from openkb.agent.tools import write_kb_file
import openkb.model_outputs as outputs
root = Path(sys.argv[1])
if sys.argv[2] == 'True':
    original = outputs.atomic_write_text
    def crash_write(path, text):
        original(path, text)
        os._exit(17)
    outputs.atomic_write_text = crash_write
with kb_ingest_lock(root / '.openkb'), model_output_scope(root):
    write_kb_file('output/report.html', 'unfinished', str(root))
    os._exit(17)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(kb_dir), str(during_write)], env=os.environ.copy()
    )
    assert result.returncode == 17
    assert artifact.read_text() == "unfinished"
    with kb_read_lock(kb_dir / ".openkb"):
        assert artifact.read_text() == ("original" if during_write else "unfinished")


def test_session_save_failure_keeps_disk_and_in_memory_completed_history(kb_dir, monkeypatch):
    import openkb.agent.chat_session as storage

    session = ChatSession.new(kb_dir, "openai/test", "en")
    session.record_turn("old", "old answer", [])
    original = storage.atomic_write_text

    def fail_after_write(path, text):
        original(path, text)
        raise OSError("disk reported failure")

    monkeypatch.setattr(storage, "atomic_write_text", fail_after_write)
    with pytest.raises(OSError):
        session.record_turn("failed", "incomplete", [])
    assert session.user_turns == ["old"]
    assert load_session(kb_dir, session.id).user_turns == ["old"]


@pytest.mark.asyncio
async def test_query_reports_saved_answer_if_later_log_write_fails(kb_dir, monkeypatch):
    import openkb.agent.query as query
    import openkb.application.conversations as conversations

    async def events(*args, **kwargs):
        yield {"event": "final", "data": {"answer": "Retained answer"}}

    def fail_log(*args):
        raise OSError("log unavailable")

    monkeypatch.setattr(query, "iter_agent_response_events", events)
    monkeypatch.setattr(conversations, "append_log", fail_log)
    result = await ask_question(kb_dir, "A question", save=True)
    path = kb_dir / "wiki/explorations/a-question.md"
    assert result.status == "failed"
    assert result.saved_path == str(path)
    assert str(path) in result.resources
    assert "Retained answer" in path.read_text()


@pytest.mark.asyncio
async def test_artifact_event_waits_for_commit_before_delivery(kb_dir, monkeypatch):
    from openkb.agent import chat

    path = kb_dir / "output/report.html"

    async def events(*args, **kwargs):
        write_kb_file("output/report.html", "Saved", str(kb_dir))
        yield {"event": "artifact", "data": {"path": "output/report.html"}}
        yield {"event": "delta", "data": {"text": "answer"}}
        yield {"event": "final", "data": {"answer": "answer", "history": []}}

    monkeypatch.setattr(chat, "iter_agent_response_events", events)
    session = ChatSession.new(kb_dir, "test", "en")
    seen = []
    async for event in chat.iter_chat_turn_events(None, session, "new"):
        seen.append(event["event"])
        if event["event"] == "artifact":
            assert path.read_text() == "Saved"
            assert not list((kb_dir / ".openkb/journal").glob("*.json"))
    assert seen == ["artifact", "delta", "final"]


@pytest.mark.asyncio
async def test_copied_tool_context_expires_without_granting_general_lock_reentrancy(kb_dir):
    import time

    from openkb.locks import async_kb_lock
    from openkb.model_outputs import model_output_scope

    finish, checked = asyncio.Event(), asyncio.Event()

    async def late_tool():
        # Copying the explicit file writer does not give a sibling operation
        # ownership of the full KB lease.
        with pytest.raises(TimeoutError):
            async with async_kb_lock(
                kb_dir / ".openkb",
                exclusive=True,
                deadline=time.monotonic() + 0.01,
            ):
                pytest.fail("Sibling unexpectedly acquired its parent's lease")
        checked.set()
        await finish.wait()
        with pytest.raises(RuntimeError, match="finished"):
            write_kb_file("output/late.html", "late", str(kb_dir))

    async with async_kb_lock(kb_dir / ".openkb", exclusive=True):
        with model_output_scope(kb_dir):
            task = asyncio.create_task(late_tool())
            await asyncio.wait_for(checked.wait(), 2)
        finish.set()
        await asyncio.wait_for(task, 2)
    assert not (kb_dir / "output/late.html").exists()


@pytest.mark.asyncio
async def test_html_critique_protects_other_files_and_stops_at_repair_barrier(kb_dir, monkeypatch):
    from openkb.application.skill_maintenance import critique_artifact
    from openkb.mutation import RecoveryRequired

    definition = kb_dir / "skills/openkb-html-critic/SKILL.md"
    definition.parent.mkdir(parents=True)
    definition.write_text("---\nname: openkb-html-critic\ndescription: Review HTML\n---\nReview")
    artifact = kb_dir / "output/report.html"
    artifact.parent.mkdir(exist_ok=True)
    artifact.write_text("original")
    other = kb_dir / "output/other.html"
    other.write_text("keep")
    calls = []

    async def model(*args, **kwargs):
        calls.append(True)
        await asyncio.to_thread(write_kb_file, "output/report.html", "reviewed", str(kb_dir))
        assert write_kb_file("output/other.html", "erase", str(kb_dir)).startswith("Access denied")
        raise RuntimeError("provider failed after writing")

    monkeypatch.setattr(Runner, "run", model)
    with pytest.raises(RuntimeError):
        await critique_artifact(kb_dir, "output/report.html")
    assert artifact.read_text() == "reviewed"
    assert other.read_text() == "keep"
    (kb_dir / ".openkb/needs-repair.json").write_text("{}")
    with pytest.raises(RecoveryRequired):
        await critique_artifact(kb_dir, "output/report.html")
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_marker", [False, True])
async def test_sdk_cannot_swallow_failed_rollback_and_commit_a_chat_turn(
    kb_dir, monkeypatch, fail_marker
):
    from openkb import model_outputs, mutation

    session = ChatSession.new(kb_dir, "openai/test", "en")
    session.record_turn("old", "saved", [])

    def fail_write(*args):
        raise OSError("write failed")

    def fail_rollback(*args):
        raise OSError("rollback failed")

    class ModelRun:
        is_complete = False
        final_output = "The model continued despite tool errors"

        async def stream_events(self):
            # Mirrors the SDK function_tool exception-to-text behavior.
            for path in ("output/first.html", "output/second.html"):
                try:
                    write_kb_file(path, "new", str(kb_dir))
                except mutation.RecoveryRequired:
                    pass
            self.is_complete = True
            if False:
                yield

        def to_input_list(self):
            return []

    monkeypatch.setattr(model_outputs, "atomic_write_text", fail_write)
    monkeypatch.setattr(mutation.MutationSnapshot, "rollback", fail_rollback)
    original_json_write = mutation.atomic_write_json

    def write_marker(path, value):
        if fail_marker and path.name == "needs-repair.json":
            raise OSError("repair marker could not be written")
        return original_json_write(path, value)

    monkeypatch.setattr(mutation, "atomic_write_json", write_marker)
    monkeypatch.setattr(Runner, "run_streamed", lambda *args, **kw: ModelRun())
    result = await continue_conversation(kb_dir, "new", session_id=session.id)
    assert result.status == "blocked"
    assert result.turn_count == 1
    assert load_session(kb_dir, session.id).user_turns == ["old"]
    assert not (kb_dir / "output/second.html").exists()
    assert (kb_dir / ".openkb/needs-repair.json").exists() is not fail_marker
    assert list((kb_dir / ".openkb/journal").glob("*.json"))
