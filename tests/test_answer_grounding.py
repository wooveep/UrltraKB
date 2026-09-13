"""A valid citation cannot authorize unsupported explanations or missing requested rows."""

import json

import pytest

from openkb.application.conversations import ask_question, continue_conversation
from openkb.locks import atomic_write_text


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [ask_question, continue_conversation])
@pytest.mark.parametrize("repairs", [True, False])
async def test_semantic_review_repairs_once_and_rechecks_before_completion(
    kb_dir, model_service, operation, repairs
):
    target = "sources/snapshots/v-p.md#block-row"
    source = (
        "Component | Port | Type\ncollector | 4100 | container network\n"
        "maintenance | 6300 | 127.0.0.1\n[Original](" + target + ")"
    )
    atomic_write_text(kb_dir / "wiki/sources/ports.md", source)
    bad = "collector: 4100, reachable from any host. [Original](" + target + ")"
    good = (
        "collector: 4100, type: container network; actual access is not defined. "
        "maintenance: 6300, type: 127.0.0.1. [Original](" + target + ")"
    )
    reviews = []
    drafts = []

    def chat(body):
        try:
            payload = json.loads(body["messages"][-1]["content"])
        except ValueError:
            payload = {}
        if payload.get("stage") == "answer_verification":
            assert not body.get("tools")
            assert "Both components" in payload["question"]
            assert source in json.dumps(payload["observations"], ensure_ascii=False).replace(
                "\\n", "\n"
            )
            reviews.append(payload["answer"])
            issues = (
                []
                if payload["answer"] == good
                else [
                    {
                        "kind": "unsupported",
                        "claim": "reachable from any host",
                        "reason": "A type label is not evidence of actual network access.",
                    },
                    {"kind": "missing", "claim": "", "reason": "maintenance row is missing."},
                ]
            )
            return {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "verdict": "unsupported" if issues else "supported",
                        "issues": issues,
                        "units": [
                            {
                                "id": unit["id"],
                                "verdict": "unsupported"
                                if (issues and "reachable from any host" in unit["text"])
                                else "supported",
                                "support": [{"observation": "o1", "quote": source}],
                            }
                            for unit in payload["units"]
                        ],
                    }
                ),
            }
        observed = any(m["role"] == "tool" for m in body["messages"])
        if not observed:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "read",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "sources/ports.md"}),
                        },
                    }
                ],
            }
        if drafts:
            assert not body.get("tools")
        answer = good if drafts and repairs else bad
        drafts.append(answer)
        return {"role": "assistant", "content": answer}

    model_service.chat_response = chat
    model_service.answer_review_response = chat
    model_service.chat_without_tools = True
    result = await operation(kb_dir, "Both components: list the port and literal type.")
    assert len(reviews) == 2
    assert len(drafts) == 2
    assert result.usage["observable_attempts"] == 5
    assert result.usage["charged_tokens"] == 650
    if repairs:
        assert result.status == "completed", result
        assert result.answer == good
        if operation is continue_conversation:
            from openkb.agent.chat_session import load_session

            saved = load_session(kb_dir, result.session_id)
            assert saved.assistant_texts == [good]
            assert bad not in json.dumps(saved.history)
            assert not any(row.get("role") == "developer" for row in saved.history)
    else:
        assert result.status != "completed"
        assert result.saved_path is None
        assert result.turn_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "review",
    [
        '{"verdict":"supported","issues":[]}',
        '{"verdict":"supported","issues":[],"verdict":"unsupported"}',
        '{"verdict":"supported","issues":[{"kind":"missing","claim":"","reason":"missing row"}]}',
        '{"verdict":"uncertain","issues":[]}',
        '{"verdict":"unsupported","issues":[{"kind":"unsupported",'
        '"claim":"invented draft text","reason":"unsupported"}]}',
        '{"verdict":"supported","issues":[],"units":[]}',
        '{"verdict":"supported","issues":[],"units":[{"id":"u1","verdict":"supported",'
        '"support":[{"observation":"o1","quote":"All hosts may connect"}]}]}',
        '{"verdict":"supported","issues":[],"units":[{"id":"u1","verdict":"supported",'
        '"support":[{"observation":"unobserved","quote":"Port: 4100"}]}]}',
    ],
)
async def test_invalid_review_never_authorizes_a_completed_answer(kb_dir, model_service, review):
    target = "sources/snapshots/v-p.md#block-row"
    atomic_write_text(kb_dir / "wiki/index.md", f"Port: 4100. [Original]({target})")

    def chat(body):
        if any(m["role"] == "tool" for m in body["messages"]):
            return {"role": "assistant", "content": f"Port: 4100 [Original]({target})"}
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "read",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"index.md"}',
                    },
                }
            ],
        }

    model_service.chat_response = chat
    model_service.answer_review_response = lambda body: {"role": "assistant", "content": review}
    result = await ask_question(kb_dir, "Which port?", save=True)
    assert result.status != "completed"
    assert result.saved_path is None
    assert result.usage["observable_attempts"] == 3


@pytest.mark.asyncio
async def test_uncited_claim_from_concept_page_cannot_skip_source_review(kb_dir, model_service):
    bad = "All hosts may connect"
    atomic_write_text(
        kb_dir / "wiki/concepts/ports.md", "Generated summary without original proof."
    )
    reviews = []

    def chat(body):
        if any(m["role"] == "tool" for m in body["messages"]):
            return {"role": "assistant", "content": bad}
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "concept",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"concepts/ports.md"}',
                    },
                }
            ],
        }

    def review(body):
        payload = json.loads(body["messages"][-1]["content"])
        reviews.append(payload)
        return {
            "role": "assistant",
            "content": json.dumps(
                {
                    "verdict": "unsupported",
                    "units": [{"id": "u1", "verdict": "unsupported", "support": []}],
                    "issues": [
                        {
                            "kind": "unsupported",
                            "claim": bad,
                            "reason": "The concept summary has no original support for this claim.",
                        }
                    ],
                }
            ),
        }

    model_service.chat_response = chat
    model_service.chat_without_tools = True
    model_service.answer_review_response = review
    result = await ask_question(kb_dir, "Who can connect?", save=True)
    assert result.status != "completed"
    assert result.saved_path is None
    assert len(reviews) == 2
