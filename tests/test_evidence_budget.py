import asyncio
import json

import pytest


def test_sdk_request_limit_stops_repeated_reads_and_keeps_review_budget(kb_dir, monkeypatch):
    from litellm import ModelResponse

    from openkb.agent.query import run_query

    (kb_dir / "wiki/sources/manual.md").write_text("Compute nodes use VIP after admission.")
    calls = []
    monkeypatch.setattr("openkb.agent.evidence_budget.MAX_GENERATION_REQUESTS", 1)

    async def provider(**kwargs):
        calls.append(kwargs)
        review = "final evidence reviewer" in str(kwargs["messages"])
        message = (
            {"content": '{"units":[]}'}
            if review
            else {
                "tool_calls": [
                    {
                        "id": "read",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "sources/manual.md"}),
                        },
                    }
                ]
            }
        )
        return ModelResponse(
            choices=[
                {
                    "message": {"role": "assistant", **message},
                    "finish_reason": "stop" if review else "tool_calls",
                }
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        )

    monkeypatch.setattr("litellm.acompletion", provider)
    answer = asyncio.run(run_query("Compute NTP?", kb_dir, "openai/test"))
    assert "预算上限" in answer and len(calls) == 2
    audits = list((kb_dir / ".openkb/answer-audits").glob("*.json"))
    audit = json.loads(audits[0].read_text())
    assert audit["budget"]["requests"] == 2
    assert audit["budget"]["observed_tokens"] == 24


@pytest.mark.asyncio
async def test_repeated_tool_payloads_consume_question_budget(kb_dir, monkeypatch):
    from agents import Agent
    from test_answer_evidence_regression import invoke, selection

    from openkb.agent.evidence_session import EvidenceSession
    from openkb.agent.query_evidence import restrict_query_agent

    (kb_dir / "wiki/sources/manual.md").write_text("Unique rule " * 100)
    chosen = selection(kb_dir)
    session = EvidenceSession(chosen)
    agent = restrict_query_agent(Agent(name="fixture"), chosen, session=session)
    first = await invoke(agent, "read_file", path="sources/manual.md")
    monkeypatch.setattr("openkb.agent.evidence_budget.MAX_TOOL_BYTES", session.budget.tool_bytes)
    second = await invoke(agent, "read_file", path="sources/manual.md")
    assert "Unique rule" in first and "budget exhausted" in second
    assert len(session.reads) == 1 and session.budget.stop_reason == "question_tool_budget"
