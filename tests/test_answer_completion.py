"""A provider length stop must never become a saved, completed answer."""

import pytest

from openkb.application.conversations import ask_question


@pytest.mark.asyncio
@pytest.mark.parametrize("recovers", [False, True])
@pytest.mark.parametrize(
    "bad_link",
    [
        "B#block-invented-id",
        "./sources/missing.md",
        "sources/snapshots/version-parse.md%23block-exact-original-id",
    ],
)
async def test_invented_source_target_reuses_observed_evidence_once(
    kb_dir, model_service, recovers, bad_link
):
    import json

    from openkb.locks import atomic_write_text

    target = "sources/snapshots/version-parse.md#block-exact-original-id"
    atomic_write_text(kb_dir / "wiki/index.md", f"Port is 4321. [Source]({target})")

    def respond(body):
        if len(model_service) == 1:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "read-evidence",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "index.md"}),
                        },
                    }
                ],
            }
        if len(model_service) == 3:
            assert not body.get("tools")
            assert any(m.get("role") == "tool" for m in body["messages"])
        link = target if recovers and len(model_service) == 3 else bad_link
        return {"role": "assistant", "content": f"Port is 4321. [Source]({link})"}

    model_service.chat_response = respond
    model_service.chat_without_tools = True
    result = await ask_question(kb_dir, "Which port?", save=True)
    expected = 3  # One retrieval, one draft, and at most one citation recovery.
    assert len(model_service) == expected
    assert result.usage["observable_attempts"] == expected
    assert result.usage["charged_tokens"] == 130 * expected
    if recovers:
        assert result.status == "completed", result
        assert f"]({target})" in result.answer
        assert "B#block" not in result.answer
    else:
        assert result.status != "completed", result
        assert result.saved_path is None


@pytest.mark.asyncio
async def test_literal_markdown_examples_are_not_evidence_links(kb_dir, model_service):
    answer = (
        "Use `[Source](sources/example.md#block-example)` or:\n\n"
        "```markdown\n[x](sources/example.md#block-example)\n```"
    )
    model_service.chat_response = lambda body: {"role": "assistant", "content": answer}
    model_service.chat_without_tools = True
    result = await ask_question(kb_dir, "Show Markdown syntax", save=True)
    assert result.status == "completed", result
    assert len(model_service) == 1
    assert result.answer == answer


@pytest.mark.asyncio
@pytest.mark.parametrize("native_source", [False, True])
async def test_complete_binding_in_one_read_can_supply_canonical_citation(
    kb_dir, model_service, native_source
):
    import json

    from openkb.locks import atomic_write_text

    reference = {"source_id": "s", "version_id": "v", "parse_id": "p", "block_id": "b"}
    text = (
        'sources/snapshots/v-p.md\n<a id="block-b"></a>\nPort: 4321.\n<!-- source-evidence: '
        + json.dumps(reference)
        + " -->"
    )
    if native_source:
        text = (
            "sources/Document-s.md\n<!-- source-evidence: " + repr(reference) + " -->\nPort: 4321."
        )
    atomic_write_text(kb_dir / "wiki/index.md", text)

    def respond(body):
        if len(model_service) == 1:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "read-binding",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "index.md"}),
                        },
                    }
                ],
            }
        return {
            "role": "assistant",
            "content": "Port: 4321. [Source](sources/snapshots/v-p.md#block-b)",
        }

    model_service.chat_response = respond
    model_service.chat_without_tools = True
    result = await ask_question(kb_dir, "Which port?", save=True)
    assert result.status == "completed", result
    assert len(model_service) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["query", "tty"])
async def test_terminal_returns_final_answer_without_intermediate_bad_citation(
    kb_dir, model_service, entry
):
    import json

    from openkb.agent.query import build_run_config_from_bundle, run_query
    from openkb.application.execution import ExecutionContext
    from openkb.locks import kb_ingest_lock

    def respond(body):
        if len(model_service) == 1:
            return {
                "role": "assistant",
                "content": "Checking [draft](B#block-wrong).",
                "tool_calls": [
                    {
                        "id": "read-before-answer",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "index.md"}),
                        },
                    }
                ],
            }
        return {"role": "assistant", "content": "Final supported answer."}

    model_service.chat_response = respond
    with kb_ingest_lock(kb_dir / ".openkb"), ExecutionContext().begin(kb_dir) as bundle:
        config = build_run_config_from_bundle("openai/offline-test", bundle)
        if entry == "query":
            answer = await run_query(
                "Question",
                kb_dir,
                "openai/offline-test",
                stream=True,
                run_config=config,
                bundle=bundle,
            )
        else:
            from openkb.agent.chat import _build_style, _stream_tty_turn
            from openkb.agent.chat_session import ChatSession
            from openkb.agent.query import build_chat_agent

            session = ChatSession.new(kb_dir, "openai/offline-test", "en")
            agent = build_chat_agent(kb_dir, session.model, bundle=bundle)
            agent.model = config.model
            answer, _ = await _stream_tty_turn(
                agent, session, "Question", _build_style(False), use_color=False
            )
    assert answer == "Final supported answer."
    assert len(model_service) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("recovers", [False, True])
async def test_length_stop_reuses_evidence_for_one_bounded_replacement(
    kb_dir, model_service, recovers
):
    def respond(body):
        second = len(model_service) > 1
        model_service.finish_reason = "stop" if second and recovers else "length"
        return {
            "role": "assistant",
            "content": "Complete answer." if second and recovers else "Partial [source](sources/",
        }

    model_service.chat_response = respond
    model_service.chat_without_tools = True
    result = await ask_question(kb_dir, "What is supported?", save=True)
    assert len(model_service) == 2
    assert result.usage["observable_attempts"] == 2
    assert result.usage["charged_tokens"] == 260
    assert not model_service[1].get("tools")
    if recovers:
        assert result.status == "completed", result
        assert result.answer == "Complete answer."
        assert result.saved_path
        from pathlib import Path

        assert "Partial" not in Path(result.saved_path).read_text()
    else:
        assert result.status != "completed", result
        assert result.saved_path is None
        assert not list((kb_dir / "wiki/explorations").glob("*.md"))


@pytest.mark.asyncio
@pytest.mark.parametrize("recovers", [False, True])
@pytest.mark.parametrize("request_limit", [None, 3, 4])
async def test_one_recovery_does_not_restart_for_a_second_failure(
    kb_dir, model_service, recovers, request_limit
):
    import json

    from openkb.locks import atomic_write_text

    target = "sources/snapshots/version-parse.md#block-supported"
    atomic_write_text(kb_dir / "wiki/index.md", f"Port is 4321. [Source]({target})")

    def respond(body):
        call = len(model_service)
        if call == 1:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "read-evidence",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "index.md"}),
                        },
                    }
                ],
            }
        model_service.finish_reason = "length" if call == 2 else "stop"
        if call >= 3:
            assert not body.get("tools") and body.get("tool_choice") == "none"
            assert any(m.get("role") == "tool" for m in body["messages"])
        link = target if call == 3 and recovers else "sources/missing.md"
        return {"role": "assistant", "content": f"Port is 4321. [Source]({link})"}

    model_service.chat_response = respond
    model_service.chat_without_tools = True
    if request_limit is not None:
        from openkb.config import load_config, save_config

        config_path = kb_dir / ".openkb/config.yaml"
        config = load_config(config_path)
        config["processing"]["max_requests"] = request_limit
        save_config(config_path, config)
    result = await ask_question(kb_dir, "Which port?", save=True)
    expected = 3
    assert len(model_service) == expected
    assert result.usage["observable_attempts"] == expected
    assert result.usage["charged_tokens"] == 130 * expected
    if recovers:
        assert result.status == "completed", result
        assert result.answer == f"Port is 4321. [Source]({target})"
        assert result.saved_path
    else:
        assert result.status != "completed", result
        assert result.saved_path is None


@pytest.mark.asyncio
async def test_truncated_tool_call_never_writes_even_when_arguments_are_valid(
    kb_dir, model_service
):
    import json

    from openkb.application.conversations import continue_conversation

    def respond(body):
        if len(model_service) == 3:
            assert not any(
                "previous response hit its output limit" in str(message.get("content"))
                for message in body["messages"]
            )
        if len(model_service) == 1:
            model_service.finish_reason = "length"
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "incomplete-write",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps(
                                {"path": "output/partial.md", "content": "Bad"}
                            ),
                        },
                    }
                ],
            }
        model_service.finish_reason = "stop"
        return {"role": "assistant", "content": "Complete answer."}

    model_service.chat_response = respond
    model_service.chat_without_tools = True
    result = await continue_conversation(kb_dir, "Give a concise answer.")
    assert result.status == "completed", result
    assert len(model_service) == 2
    assert not (kb_dir / "output/partial.md").exists()
    assert str(kb_dir / "output/partial.md") not in result.resources
    following = await continue_conversation(kb_dir, "A new question", session_id=result.session_id)
    assert following.status == "completed", following
    assert len(model_service) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_legacy_query_does_not_return_a_truncated_answer_as_complete(
    kb_dir, model_service, stream
):
    from openkb.agent.query import build_run_config_from_bundle, run_query
    from openkb.application.execution import ExecutionContext
    from openkb.processing import OutputTruncated

    model_service.finish_reason = "length"
    model_service.chat_response = lambda body: {"role": "assistant", "content": "Partial"}
    model_service.chat_without_tools = True
    from openkb.locks import kb_ingest_lock

    with kb_ingest_lock(kb_dir / ".openkb"), ExecutionContext().begin(kb_dir) as bundle:
        with pytest.raises(OutputTruncated):
            await run_query(
                "A question",
                kb_dir,
                "openai/offline-test",
                stream=stream,
                run_config=build_run_config_from_bundle("openai/offline-test", bundle),
                bundle=bundle,
            )
    assert len(model_service) == (2 if stream else 1)


@pytest.mark.asyncio
async def test_tty_chat_does_not_commit_a_length_stopped_turn(kb_dir, model_service):
    from openkb.agent.chat import _build_style, _run_turn
    from openkb.agent.chat_session import ChatSession, load_session
    from openkb.agent.query import build_chat_agent, build_run_config_from_bundle
    from openkb.application.execution import ExecutionContext
    from openkb.locks import kb_ingest_lock, session_lock
    from openkb.processing import OutputTruncated

    model_service.finish_reason = "length"
    model_service.chat_response = lambda body: {"role": "assistant", "content": "Partial"}
    model_service.chat_without_tools = True
    session = ChatSession.new(kb_dir, "openai/offline-test", "en")
    session.record_turn("Old question", "Old complete answer", [])
    with (
        session_lock(kb_dir, session.id),
        kb_ingest_lock(kb_dir / ".openkb"),
        ExecutionContext().begin(kb_dir) as bundle,
    ):
        agent = build_chat_agent(kb_dir, session.model, bundle=bundle)
        agent.model = build_run_config_from_bundle(session.model, bundle).model
        with pytest.raises(OutputTruncated):
            await _run_turn(agent, session, "New question", _build_style(False), use_color=False)
    assert len(model_service) == 2
    assert load_session(kb_dir, session.id).user_turns == ["Old question"]


@pytest.mark.asyncio
async def test_replacement_cannot_reopen_tool_work(kb_dir, model_service):
    import json

    def respond(body):
        if len(model_service) == 1:
            model_service.finish_reason = "length"
            return {"role": "assistant", "content": "Partial"}
        assert not body.get("tools")
        model_service.finish_reason = "stop"
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "unavailable-read",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "index.md"}),
                    },
                }
            ],
        }

    model_service.chat_response = respond
    model_service.chat_without_tools = True
    result = await ask_question(kb_dir, "A question", save=True)
    assert result.status != "completed", result
    assert result.saved_path is None
    assert len(model_service) == 2
    assert model_service[1].get("tool_choice") == "none"


@pytest.mark.asyncio
async def test_closing_replacement_waits_for_its_stream_cleanup(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from agents import Agent, RawResponsesStreamEvent, Runner
    from openai.types.responses import ResponseTextDeltaEvent

    from openkb.agent.query import iter_agent_response_events

    closed = asyncio.Event()
    runs = []

    class Run:
        is_complete = False
        final_output = "Partial"

        def __init__(self, replacement):
            self.replacement = replacement
            self.raw_responses = [SimpleNamespace(output=[SimpleNamespace(status="incomplete")])]

        async def stream_events(self):
            if not self.replacement:
                self.is_complete = True
                return
            try:
                yield RawResponsesStreamEvent(
                    data=ResponseTextDeltaEvent(
                        type="response.output_text.delta",
                        delta="Replacement",
                        content_index=0,
                        item_id="message",
                        output_index=0,
                        sequence_number=0,
                        logprobs=[],
                    )
                )
            finally:
                await asyncio.sleep(0)
                closed.set()

        def cancel(self, **kwargs):
            self.is_complete = True

        def to_input_list(self):
            return [{"role": "user", "content": "A question"}]

    def run(*args, **kwargs):
        value = Run(bool(runs))
        runs.append(value)
        return value

    monkeypatch.setattr(Runner, "run_streamed", run)
    stream = iter_agent_response_events(Agent(name="Test"), "A question")
    while (event := await anext(stream))["event"] == "status":
        pass
    assert event["data"]["text"] == "Replacement"
    await stream.aclose()
    assert closed.is_set()
