import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [True, False])
async def test_chat_note_never_publishes_the_unverified_draft(kb_dir, accepted):
    from agents import Agent, function_tool
    from test_answer_evidence_regression import invoke, selection

    from openkb.agent.answer_finalization import AnswerDecision
    from openkb.agent.evidence_session import EvidenceSession
    from openkb.agent.query_evidence import restrict_query_agent
    from openkb.agent.tools import write_kb_file

    target = kb_dir / "wiki/explorations/ntp.md"
    target.parent.mkdir(exist_ok=True)
    target.write_text("previous verified note")

    @function_tool
    def write_file(path: str, content: str) -> str:
        """Write a note."""
        return write_kb_file(path, content, str(kb_dir))

    chosen = selection(kb_dir)
    session = EvidenceSession(chosen)
    agent = restrict_query_agent(Agent(name="fixture", tools=[write_file]), chosen, session=session)
    reply = await invoke(
        agent, "write_file", path="wiki/explorations/ntp.md", content="unverified external NTP"
    )
    assert "Queued" in reply and target.read_text() == "previous verified note"
    await session.writes.publish(
        AnswerDecision("verified compute VIP", "answered" if accepted else "insufficient_evidence")
    )
    assert target.read_text() == ("verified compute VIP" if accepted else "previous verified note")
