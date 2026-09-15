"""Answer citations and located corrections through the shared conversation entry point."""

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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    [
        None,
        "omit_empty",
        "null_insertions",
        "unknown_field",
        "outside_scope",
        "unknown",
        "duplicate",
        "rewrite",
    ],
)
async def test_located_repair_preserves_table_conditions_figures_and_history(
    kb_dir, model_service, damage
):
    from openkb.locks import atomic_write_text
    from tests.http_model_fixture import answer_review_response

    target = "sources/snapshots/v-p.md#block-table"
    conditions = (
        "## Conditions\n\nStop before changing pressure.\n\nUse standby mode only for row 3."
    )
    table = "| Row | Value | Mode |\n| --- | --- | --- |\n" + "\n".join(
        f"| {i} | {7300 + i} | {'standby' if i == 3 else 'normal'} |" for i in range(1, 7)
    )
    figures = (
        "Left: inlet. ![Inlet](sources/images/inlet.png)\n\n"
        "Right: outlet. ![Outlet](sources/images/outlet.png)"
    )
    good = conditions + "\n\n## Values\n\n" + table + "\n\n" + figures + f"\n\n[Original]({target})"
    bad = good.replace("| 3 | 7303 | standby |", "| 3 | 7303 | public access |")
    bad = bad.replace("![Inlet](sources/images/inlet.png)", "![Inlet](sources/images/outlet.png)")
    atomic_write_text(kb_dir / "wiki/sources/minimal.md", good)
    reviews, corrections = [], []

    def chat(body):
        try:
            payload = json.loads(body["messages"][-1]["content"])
        except ValueError:
            payload = {}
        if payload.get("stage") == "answer_correction":
            corrections.append(payload)
            assert not body.get("tools")
            by_id = {row["id"]: row["text"] for row in payload["units"]}
            edits = []
            for identity in payload["editable_units"]:
                text = (
                    by_id[identity]
                    .replace("public access", "standby")
                    .replace(
                        "![Inlet](sources/images/outlet.png)", "![Inlet](sources/images/inlet.png)"
                    )
                )
                edits.append({"unit": identity, "text": text})
            if damage == "outside_scope":
                unaffected = next(row for row in payload["units"] if "Stop before" in row["text"])
                edits.append({"unit": unaffected["id"], "text": "Change pressure while running."})
            if damage == "unknown":
                edits[0]["unit"] = "u-unobserved"
            if damage == "duplicate":
                edits.append(edits[0])
            patch = {"edits": edits, "insertions": []}
            if damage == "omit_empty":
                patch.pop("insertions")
            if damage == "null_insertions":
                patch["insertions"] = None
            if damage == "unknown_field":
                patch["replacement"] = good
            return {
                "role": "assistant",
                "content": good if damage == "rewrite" else json.dumps(patch),
            }
        if any(m["role"] == "tool" for m in body["messages"]):
            return {"role": "assistant", "content": bad}
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "source",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"sources/minimal.md"}',
                    },
                }
            ],
        }

    def review(body):
        payload = json.loads(body["messages"][-1]["content"])
        reviews.append(payload["answer"])
        if payload["answer"] == good:
            return {"role": "assistant", "content": json.dumps(answer_review_response(payload))}
        claims = ["public access", "![Inlet](sources/images/outlet.png)"]
        claims = [
            claim for claim in claims if any(claim in row["text"] for row in payload["units"])
        ]
        return {
            "role": "assistant",
            "content": json.dumps(
                {
                    "verdict": "unsupported" if claims else "supported",
                    "issues": [
                        {
                            "kind": "image" if claim.startswith("!") else "unsupported",
                            "claim": claim,
                            "reason": "Use the observed row condition and exact caption.",
                        }
                        for claim in claims
                    ],
                    "units": [
                        {
                            "id": row["id"],
                            "verdict": "unsupported"
                            if any(c in row["text"] for c in claims)
                            else "supported",
                            "support": [{"observation": "o1", "quote": good}],
                        }
                        for row in payload["units"]
                    ],
                }
            ),
        }

    model_service.chat_response = chat
    model_service.answer_review_response = review
    model_service.chat_without_tools = True
    result = await continue_conversation(
        kb_dir, "List all six rows, conditions and captioned figures."
    )
    assert len(corrections) == 1
    rejected = damage not in {None, "omit_empty"}
    assert result.usage["observable_attempts"] == 3 + len(reviews)
    if rejected:
        assert result.status != "completed" and result.turn_count == 0
        assert result.error.endswith("(answer_correction_invalid)")
        assert reviews and set(reviews) == {bad}
    else:
        assert result.status == "completed", result
        assert result.answer == good
        assert set(reviews) == {bad, good} and reviews.index(good) > 0
        from openkb.agent.chat_session import load_session

        saved = load_session(kb_dir, result.session_id)
        assert saved.assistant_texts == [good]
        assert not any(row.get("role") == "developer" for row in saved.history)


@pytest.mark.asyncio
@pytest.mark.parametrize("patch_kind", ["insertions_only", "missing_operations", "null_edits"])
async def test_missing_answer_coverage_accepts_only_explicit_valid_insertions(
    kb_dir, model_service, patch_kind
):
    from openkb.locks import atomic_write_text
    from tests.http_model_fixture import answer_review_response

    first, missing = "Stop before replacement.", "Use a safety shield."
    complete = first + "\n" + missing
    atomic_write_text(kb_dir / "wiki/sources/steps.md", complete)
    reviews = []

    def chat(body):
        try:
            payload = json.loads(body["messages"][-1]["content"])
        except ValueError:
            payload = {}
        if payload.get("stage") == "answer_correction":
            assert payload["insertions_allowed"] and not payload["editable_units"]
            patch = {"insertions": [{"after": "u1", "text": missing}]}
            if patch_kind == "missing_operations":
                patch = {}
            elif patch_kind == "null_edits":
                patch["edits"] = None
            return {"role": "assistant", "content": json.dumps(patch)}
        if any(m["role"] == "tool" for m in body["messages"]):
            return {"role": "assistant", "content": first}
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "original",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"sources/steps.md"}'},
                }
            ],
        }

    def review(body):
        payload = json.loads(body["messages"][-1]["content"])
        reviews.append(payload["answer"])
        value = answer_review_response(payload)
        if payload["answer"] == first:
            value.update(
                verdict="unsupported",
                issues=[
                    {
                        "kind": "missing",
                        "units": [],
                        "claim": "",
                        "reason": "Include the observed safety shield step.",
                    }
                ],
            )
        return {"role": "assistant", "content": json.dumps(value)}

    model_service.chat_response = chat
    model_service.answer_review_response = review
    model_service.chat_without_tools = True
    result = await continue_conversation(kb_dir, "List both replacement precautions.")
    if patch_kind == "insertions_only":
        assert result.status == "completed" and result.answer == complete
        assert reviews == [first, complete]
    else:
        assert result.status == "failed" and result.turn_count == 0
        assert reviews == [first]


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
