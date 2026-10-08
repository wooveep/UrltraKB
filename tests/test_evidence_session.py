import json

import pytest
from test_answer_evidence_regression import invoke, selection


@pytest.mark.asyncio
async def test_whole_quote_cannot_join_two_subjects_and_failed_slot_stops(kb_dir):
    from agents import Agent

    from openkb.agent.evidence_session import EvidenceSession
    from openkb.agent.query_evidence import restrict_query_agent

    quote = (
        "Compute nodes use VIP after admission. Management nodes use external NTP when configured."
    )
    (kb_dir / "wiki/sources/manual.json").write_text(json.dumps([{"page": 1, "content": quote}]))
    chosen = selection(kb_dir)
    session = EvidenceSession(chosen)
    agent = restrict_query_agent(Agent(name="fixture"), chosen, session=session)
    await invoke(agent, "get_page_content", doc_name="manual", pages="1", part=None)
    values = dict(
        subject="Compute nodes",
        setting="VIP",
        condition="when configured",
        quote=quote,
        locator="sources/manual.json pages=1 part=None",
    )
    bad = await invoke(agent, "record_source_fact", **values)
    assert "Unverified" in bad and not session.facts
    # Rewording the quote cannot buy a new retry slot for the same source rule.
    second = await invoke(agent, "record_source_fact", **{**values, "quote": quote[:-1]})
    assert "Unverified" in second
    third = await invoke(agent, "record_source_fact", **{**values, "condition": "after admission"})
    assert "retry budget" in third and not session.facts


@pytest.mark.asyncio
async def test_provenance_fact_is_recorded_with_server_read_id_and_not_in_next_session(kb_dir):
    from agents import Agent

    from openkb.agent.evidence_session import EvidenceSession
    from openkb.agent.query_evidence import restrict_query_agent

    quote = "Compute nodes use VIP after admission."
    (kb_dir / "wiki/sources/manual.json").write_text(json.dumps([{"page": 1, "content": quote}]))
    chosen = selection(kb_dir)
    session = EvidenceSession(chosen)
    agent = restrict_query_agent(Agent(name="fixture"), chosen, session=session)
    await invoke(agent, "get_page_content", doc_name="manual", pages="1", part=None)
    fact = json.loads(
        await invoke(
            agent,
            "record_source_fact",
            subject="Compute nodes",
            setting="VIP",
            condition="after admission",
            quote=quote,
            locator="sources/manual.json pages=1 part=None",
        )
    )
    assert fact["fact_id"] in session.facts and fact["read_id"] in session.reads
    assert fact["status"] == "provenance_checked"
    assert not EvidenceSession(chosen).facts


@pytest.mark.asyncio
async def test_sliced_reads_and_changed_subject_do_not_reset_failed_rule(kb_dir):
    from agents import Agent

    from openkb.agent.evidence_session import EvidenceSession
    from openkb.agent.query_evidence import restrict_query_agent

    body = "Compute nodes use VIP after admission. Management nodes use external NTP."
    (kb_dir / "wiki/sources/manual.md").write_text(body)
    chosen = selection(kb_dir)
    session = EvidenceSession(chosen)
    agent = restrict_query_agent(Agent(name="fixture"), chosen, session=session)
    await invoke(agent, "read_file", path="sources/manual.md")
    first = next(iter(session.reads.values()))
    for condition in ("when configured", "before admission"):
        session.record_fact(
            first.view_id,
            subject="Compute nodes",
            setting="VIP",
            condition=condition,
            quote=body,
            locator=first.read_id,
        )
    sliced = session.register_read(
        chosen.views[0], "sources/manual.md", "chars=14:37", "use VIP after admission."
    )
    assert session.fact_exhausted(sliced.read_id, "VIP")
    assert not session.fact_exhausted(first.read_id, "Management nodes")


def test_fabricated_slot_ids_share_the_server_retry_budget_and_can_be_corrected(kb_dir):
    import re

    from openkb.agent.evidence_session import EvidenceSession

    body = "Compute nodes use VIP after admission."
    (kb_dir / "wiki/sources/manual.md").write_text(body)
    chosen = selection(kb_dir)
    session = EvidenceSession(chosen)
    read = session.register_read(chosen.views[0], "sources/manual.md", "original", body)
    values = dict(
        subject="Compute nodes",
        setting="VIP",
        condition="after admission",
        quote=body,
        locator=read.read_id,
    )
    rejected = session.record_fact(read.view_id, **values, slot_id="invented-1")
    assert "corrections_left=1" in rejected
    slot = re.search(r"slot_id=([a-f0-9]+)", rejected).group(1)
    assert session.record_fact(read.view_id, **values, slot_id="invented-2") == rejected
    # Correcting just the ID must not hit the cache for the invalid-ID request.
    assert json.loads(session.record_fact(read.view_id, **values, slot_id=slot))["slot_id"] == slot
    rejected = session.record_fact(
        read.view_id, **{**values, "condition": "before admission"}, slot_id="invented-3"
    )
    assert "corrections_left=0" in rejected
    assert session.fact_exhausted(read.read_id, "Compute nodes")
    assert "retry budget" in session.record_fact(read.view_id, **values, slot_id=slot)
