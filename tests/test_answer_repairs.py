"""Source citation rendering and follow-up history through public conversation entry points."""

import asyncio
import json

import pytest

from openkb.application.conversations import continue_conversation
from openkb.application.documents import import_document
from openkb.application.source_history import source_status


@pytest.mark.asyncio
@pytest.mark.parametrize("marker_prefix", ["", "@"])
@pytest.mark.parametrize(
    "entry,unknown",
    [
        ("conversation", False),
        ("conversation", True),
        ("tty_chat", False),
        ("tty_query", False),
    ],
)
async def test_short_evidence_citations_render_and_survive_followup(
    kb_dir, tmp_path, model_service, unknown, entry, capsys, marker_prefix
):
    source = tmp_path / "small.md"
    source.write_text("The pressure is 37 kPa; stop first before changing it.")
    imported = await asyncio.to_thread(import_document, kb_dir, source)
    navigation = source_status(kb_dir, imported.source_id)["navigation"]
    observed = []

    def chat(body):
        outputs = [m for m in body["messages"] if m["role"] == "tool"]
        if not outputs:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "original",
                        "type": "function",
                        "function": {
                            "name": "read_source_node",
                            "arguments": json.dumps(
                                {
                                    "source_id": imported.source_id,
                                    "node_id": navigation["nodes"][0]["id"],
                                    "offset": 0,
                                    "start": 0,
                                    "max_chars": 4000,
                                }
                            ),
                        },
                    }
                ],
            }
        row = json.loads(outputs[-1]["content"])["evidence"][0]
        observed.append(row)
        citation = "[evidence:unobserved]" if unknown else row.get("short_citation", "MISSING")
        citation = citation.replace("[evidence:", f"[{marker_prefix}evidence:")
        return {"role": "assistant", "content": row["text"] + " " + citation}

    model_service.chat_response = chat
    model_service.chat_without_tools = True
    if entry != "conversation":
        from openkb.agent.chat import _build_style, _stream_tty_turn
        from openkb.agent.chat_session import ChatSession
        from openkb.agent.query import build_chat_agent, build_run_config_from_bundle, run_query
        from openkb.application.execution import ExecutionContext
        from openkb.locks import kb_ingest_lock

        context = ExecutionContext()
        with kb_ingest_lock(kb_dir / ".openkb"), context.begin(kb_dir) as bundle:
            config = build_run_config_from_bundle("openai/offline-test", bundle)
            if entry == "tty_query":
                answer = await run_query(
                    "Pressure?",
                    kb_dir,
                    "openai/offline-test",
                    stream=True,
                    raw=True,
                    run_config=config,
                    bundle=bundle,
                )
            else:
                session = ChatSession.new(kb_dir, "openai/offline-test", "en")
                agent = build_chat_agent(kb_dir, session.model, bundle=bundle)
                agent.model = config.model
                answer, history = await _stream_tty_turn(
                    agent, session, "Pressure?", _build_style(False), use_color=False, raw=True
                )
        if entry == "tty_chat":
            session.record_turn("Pressure?", answer, history)
            assert session.assistant_texts == [answer]
        assert observed[0]["citation"] in answer
        screen = capsys.readouterr().out
        assert "[evidence:" not in screen and observed[0]["citation"] in screen
        return
    first = await continue_conversation(kb_dir, "What is the pressure and prerequisite?")
    if unknown:
        assert first.status != "completed" and first.turn_count == 0
        assert first.error.endswith("(answer_citation_invalid)")
        return
    assert first.status == "completed", first
    assert observed[0]["citation"] in first.answer
    assert "[evidence:" not in first.answer
    second = await continue_conversation(
        kb_dir, "Repeat the prerequisite.", session_id=first.session_id
    )
    assert second.status == "completed", second
    assert observed[0]["citation"] in second.answer
    from openkb.agent.chat_session import load_session

    saved = load_session(kb_dir, second.session_id)
    assert saved.assistant_texts == [first.answer, second.answer]
    assert all("[evidence:" not in text for text in saved.assistant_texts)


@pytest.mark.parametrize(
    "literal",
    [
        "`{marker}`",
        "```md\n{marker}\n```",
        "~~~\n{marker}\n~~~",
        "    {marker}",
        r"\{marker}",
    ],
)
def test_reference_rendering_keeps_literal_examples_and_table_layout(literal):
    from openkb.agent.answer_references import render_references

    marker = "[evidence:example]"
    citation = "[原文](sources/snapshots/v-p.md#block-row)"
    example = literal.format(marker=marker)
    table = "| Scope | Citation |\n| --- | --- |\n| A \\| B | " + marker + " |"
    original = example + "\n\n" + table
    rendered, unresolved = render_references(original, {marker: citation})
    assert rendered == example + "\n\n" + table.replace(marker, citation)
    assert unresolved == []
