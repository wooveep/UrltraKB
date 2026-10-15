"""Completed files and completed turns have independent recovery boundaries."""

import asyncio
import os
import subprocess
import sys

import pytest

from ultrakb.agent.chat_session import ChatSession, load_session
from ultrakb.agent.tools import write_kb_file
from ultrakb.application.conversations import ask_question
from ultrakb.locks import kb_read_lock

pytest_plugins = ("test_vendor_sdk",)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["complete", "stop", "error"])
@pytest.mark.parametrize("dispatch", ["task", "thread"])
async def test_chat_keeps_completed_tool_files_without_inventing_a_turn(kb_dir, ending, dispatch):
    from ultrakb.locks import LockCancelled, async_kb_lock, async_session_lock
    from ultrakb.model_outputs import ModelOutputs, model_output_scope

    session = ChatSession.new(kb_dir, "deepseek/deepseek-flash", "en")
    session.record_turn("old", "old answer", [])
    outputs = ModelOutputs()
    try:
        async with (
            async_session_lock(kb_dir, session.id),
            async_kb_lock(kb_dir / ".ultrakb", exclusive=True),
        ):
            with model_output_scope(kb_dir, outputs):

                async def write():
                    return write_kb_file("output/report.html", "<p>Saved</p>", str(kb_dir))

                response = (
                    await asyncio.create_task(write())
                    if dispatch == "task"
                    else await asyncio.to_thread(
                        write_kb_file, "output/report.html", "<p>Saved</p>", str(kb_dir)
                    )
                )
                assert response.startswith("Written:")
                if ending == "stop":
                    raise LockCancelled("Submission stopped after its completed write")
                if ending == "error":
                    raise OSError("Delivery failed after its completed write")
                session.record_turn("new", "Completed delivery", [])
    except (LockCancelled, OSError):
        assert ending != "complete"
    assert str(kb_dir / "output/report.html") in outputs.resources
    assert "created: output/report.html" in outputs.changes
    assert load_session(kb_dir, session.id).turn_count == (2 if ending == "complete" else 1)
    assert (kb_dir / "output/report.html").read_text() == "<p>Saved</p>"
    assert not list((kb_dir / ".ultrakb/journal").glob("*.json"))


@pytest.mark.parametrize("during_write", [False, True])
def test_crash_recovers_only_uncommitted_tool_write(kb_dir, during_write):
    artifact = kb_dir / "output/report.html"
    artifact.parent.mkdir(exist_ok=True)
    artifact.write_text("original")
    script = """
import os, sys
from pathlib import Path
from ultrakb.locks import kb_ingest_lock
from ultrakb.model_outputs import model_output_scope
from ultrakb.agent.tools import write_kb_file
import ultrakb.model_outputs as outputs
root = Path(sys.argv[1])
if sys.argv[2] == 'True':
    original = outputs.atomic_write_text
    def crash_write(path, text):
        original(path, text)
        os._exit(17)
    outputs.atomic_write_text = crash_write
with kb_ingest_lock(root / '.ultrakb'), model_output_scope(root):
    write_kb_file('output/report.html', 'unfinished', str(root))
    os._exit(17)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(kb_dir), str(during_write)], env=os.environ.copy()
    )
    assert result.returncode == 17
    assert artifact.read_text() == "unfinished"
    with kb_read_lock(kb_dir / ".ultrakb"):
        assert artifact.read_text() == ("original" if during_write else "unfinished")


def test_session_save_failure_keeps_disk_and_in_memory_completed_history(kb_dir, monkeypatch):
    import ultrakb.agent.chat_session as storage

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
async def test_query_reports_saved_answer_if_later_log_write_fails(
    kb_dir, model_service, monkeypatch
):
    import ultrakb.application.conversations as conversations
    from ultrakb.locks import atomic_write_text

    atomic_write_text(
        kb_dir / ".ultrakb/config.yaml",
        "model: deepseek/deepseek-flash\nlanguage: en\nmodel_capacity:\n"
        "  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\nrole_generation:\n"
        "  answer:\n    max_tokens: 128\n    reasoning_effort: none\n",
    )
    atomic_write_text(
        kb_dir / "wiki/sources/write-fixture.md", "The retained answer is 17 credits."
    )

    def fail_log(*args, **kwargs):
        raise OSError("log unavailable")

    monkeypatch.setattr(conversations, "append_log", fail_log)
    result = await ask_question(
        kb_dir, "Read sources/write-fixture.md, then report the retained answer.", save=True
    )
    assert result.status == "failed" and result.saved_path
    from pathlib import Path

    path = Path(result.saved_path)
    assert path.is_file() and str(path) in result.resources
    assert "17" in path.read_text()
    assert result.answer in path.read_text()


@pytest.mark.asyncio
async def test_artifact_event_waits_for_commit_before_delivery(kb_dir):
    import json

    from ultrakb.agent.tools import artifact_event_from_write
    from ultrakb.locks import async_kb_lock
    from ultrakb.model_outputs import model_output_scope

    path = kb_dir / "output/report.html"
    async with async_kb_lock(kb_dir / ".ultrakb", exclusive=True):
        with model_output_scope(kb_dir):
            arguments = {"path": "output/report.html", "content": "Saved"}
            completed = write_kb_file(**arguments, kb_root=str(kb_dir))
            event = artifact_event_from_write("write_file", json.dumps(arguments), completed)
            assert event["path"] == "output/report.html"
            assert path.read_text() == "Saved"
            assert not list((kb_dir / ".ultrakb/journal").glob("*.json"))


@pytest.mark.asyncio
async def test_copied_tool_context_expires_without_granting_general_lock_reentrancy(kb_dir):
    import time

    from ultrakb.locks import async_kb_lock
    from ultrakb.model_outputs import model_output_scope

    finish, checked = asyncio.Event(), asyncio.Event()

    async def late_tool():
        # Copying the explicit file writer does not give a sibling operation
        # ownership of the full KB lease.
        with pytest.raises(TimeoutError):
            async with async_kb_lock(
                kb_dir / ".ultrakb",
                exclusive=True,
                deadline=time.monotonic() + 0.01,
            ):
                pytest.fail("Sibling unexpectedly acquired its parent's lease")
        checked.set()
        await finish.wait()
        with pytest.raises(RuntimeError, match="finished"):
            write_kb_file("output/late.html", "late", str(kb_dir))

    async with async_kb_lock(kb_dir / ".ultrakb", exclusive=True):
        with model_output_scope(kb_dir):
            task = asyncio.create_task(late_tool())
            await asyncio.wait_for(checked.wait(), 2)
        finish.set()
        await asyncio.wait_for(task, 2)
    assert not (kb_dir / "output/late.html").exists()


@pytest.mark.asyncio
async def test_html_critique_protects_other_files_and_stops_at_repair_barrier(kb_dir):
    from ultrakb.application.skill_maintenance import critique_artifact
    from ultrakb.artifact_history import preserve_artifact_history
    from ultrakb.locks import async_kb_lock
    from ultrakb.model_outputs import model_output_scope
    from ultrakb.mutation import RecoveryRequired

    artifact = kb_dir / "output/report.html"
    artifact.parent.mkdir(exist_ok=True)
    artifact.write_text("original")
    other = kb_dir / "output/other.html"
    other.write_text("keep")
    with pytest.raises(OSError):
        async with async_kb_lock(kb_dir / ".ultrakb", exclusive=True):
            with preserve_artifact_history(kb_dir, target=artifact), model_output_scope(kb_dir):
                await asyncio.to_thread(
                    write_kb_file, "output/report.html", "reviewed", str(kb_dir)
                )
                assert write_kb_file("output/other.html", "erase", str(kb_dir)).startswith(
                    "Access denied"
                )
                raise OSError("Delivery failed after writing")
    assert artifact.read_text() == "reviewed"
    assert other.read_text() == "keep"
    (kb_dir / ".ultrakb/needs-repair.json").write_text("{}")
    with pytest.raises(RecoveryRequired):
        await critique_artifact(kb_dir, "output/report.html")


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_marker", [False, True])
async def test_sdk_cannot_swallow_failed_rollback_and_commit_a_chat_turn(
    kb_dir, monkeypatch, fail_marker
):
    import json

    from agents import RunConfig
    from agents.tool_context import ToolContext

    from ultrakb import model_outputs, mutation
    from ultrakb.agent.query import build_chat_agent
    from ultrakb.locks import async_kb_lock

    session = ChatSession.new(kb_dir, "deepseek/deepseek-flash", "en")
    session.record_turn("old", "saved", [])

    def fail_write(*args):
        raise OSError("write failed")

    def fail_rollback(*args):
        raise OSError("rollback failed")

    monkeypatch.setattr(model_outputs, "atomic_write_text", fail_write)
    monkeypatch.setattr(mutation.MutationSnapshot, "rollback", fail_rollback)
    original_json_write = mutation.atomic_write_json

    def write_marker(path, value):
        if fail_marker and path.name == "needs-repair.json":
            raise OSError("repair marker could not be written")
        return original_json_write(path, value)

    monkeypatch.setattr(mutation, "atomic_write_json", write_marker)
    agent = build_chat_agent(kb_dir, "deepseek/deepseek-flash")
    write = next(tool for tool in agent.tools if tool.name == "write_file")
    with pytest.raises(mutation.RecoveryRequired):
        async with async_kb_lock(kb_dir / ".ultrakb", exclusive=True):
            with model_outputs.model_output_scope(kb_dir):
                # Invoke the genuine SDK function-tool adapter on explicit arguments.
                # Its exception-to-text conversion must not clear the writer's fatal latch.
                for path in ("output/first.html", "output/second.html"):
                    result = await write.on_invoke_tool(
                        ToolContext(
                            context=None,
                            tool_name="write_file",
                            tool_call_id=path,
                            tool_arguments=json.dumps({"path": path, "content": "new"}),
                            run_config=RunConfig(tracing_disabled=True),
                        ),
                        json.dumps({"path": path, "content": "new"}),
                    )
                    assert "error" in result.lower()
            session.record_turn("new", "This must remain unreachable", [])
    assert load_session(kb_dir, session.id).user_turns == ["old"]
    assert not (kb_dir / "output/second.html").exists()
    assert (kb_dir / ".ultrakb/needs-repair.json").exists() is not fail_marker
    assert list((kb_dir / ".ultrakb/journal").glob("*.json"))
