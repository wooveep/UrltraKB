import pytest
from test_answer_evidence_regression import selection, source


@pytest.mark.asyncio
async def test_unread_fact_cannot_escape_finalizer(kb_dir, monkeypatch):
    from agents import Agent

    from openkb.agent.answer_finalization import finalize_answer
    from openkb.agent.evidence_session import EvidenceSession

    source(kb_dir)
    session = EvidenceSession(selection(kb_dir))
    monkeypatch.setattr(
        "agents.Runner.run", lambda *a, **k: pytest.fail("No evidence review should call a model")
    )
    decision, usage = await finalize_answer(
        Agent(name="fixture"), "NTP?", "Compute nodes use external NTP.", session
    )
    assert decision.outcome == "insufficient_evidence"
    assert "use external NTP" not in decision.answer and usage is None


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered", [False, True])
async def test_image_proof_requires_actual_review_image_and_bound_region(
    kb_dir, monkeypatch, delivered
):
    import json
    from types import SimpleNamespace

    import pymupdf
    from agents import Agent
    from test_answer_evidence_regression import invoke

    from openkb.agent.answer_review import EvidenceReview, assess_review, review_answer
    from openkb.agent.evidence_session import EvidenceSession
    from openkb.agent.query_evidence import restrict_query_agent
    from openkb.llm_images import image_digest

    path = "sources/diagram.png"
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 64, 64), False)
    pix.clear_with(128)
    (kb_dir / "wiki" / path).write_bytes(pix.tobytes("png"))
    source(kb_dir, images=[path])
    chosen = selection(kb_dir)
    session = EvidenceSession(chosen)
    agent = restrict_query_agent(Agent(name="fixture"), chosen, session=session)
    await invoke(agent, "get_image", image_path=path)
    read = next(iter(session.reads.values()))
    report = EvidenceReview.model_validate(
        {
            "units": [
                {
                    "unit_id": 0,
                    "verdict": "supported",
                    "reason": "",
                    "proofs": [
                        {
                            "read_id": read.read_id,
                            "quote": "spec:\n  loadBalancerIP: address",
                            "subject": "spec",
                            "setting": "loadBalancerIP",
                            "condition": "",
                            "visual_region": [0, 0, 1, 1],
                        }
                    ],
                }
            ]
        }
    )
    answer = "The pictured field is under spec.loadBalancerIP."
    assert not assess_review(answer, session, report).accepted_units

    async def model(agent, input, **kwargs):
        assert any(
            item.get("type") == "input_image" and item["image_url"] == read.image_url
            for item in input[0]["content"]
        )
        assert read.read_id in json.dumps(input)
        return SimpleNamespace(
            final_output=report,
            raw_responses=[
                SimpleNamespace(
                    usage=None,
                    openkb_image_digests={image_digest(read.image_url)} if delivered else set(),
                )
            ],
        )

    monkeypatch.setattr("agents.Runner.run", model)
    reviewed, _ = await review_answer(agent, "Which pictured field?", answer, session)
    assert reviewed.accepted_units == ((0,) if delivered else ())
    assert session.review_audit[-1]["images_delivered"] == ([read.read_id] if delivered else [])
    report.units[0].proofs[0].visual_region = [-1, 0, 1, 1]
    assert not assess_review(answer, session, report).accepted_units


def test_review_cannot_accept_an_unread_or_fabricated_proof(kb_dir):
    from openkb.agent.answer_review import EvidenceReview, assess_review
    from openkb.agent.evidence_session import EvidenceSession

    session = EvidenceSession(selection(kb_dir))
    report = EvidenceReview.model_validate(
        {
            "units": [
                {
                    "unit_id": 0,
                    "verdict": "supported",
                    "reason": "",
                    "proofs": [
                        {
                            "read_id": "invented",
                            "quote": "anything",
                            "subject": "nodes",
                            "setting": "NTP",
                            "condition": "",
                            "visual_region": None,
                        }
                    ],
                }
            ]
        }
    )
    reviewed = assess_review("Nodes use NTP.", session, report)
    assert reviewed.issues and not reviewed.accepted_units


def test_failed_rule_cannot_reenter_answer_through_a_different_read(kb_dir):
    from openkb.agent.answer_review import EvidenceReview, assess_review
    from openkb.agent.evidence_session import EvidenceSession

    source(kb_dir)
    chosen = selection(kb_dir)
    session = EvidenceSession(chosen)
    quote = "Compute nodes use VIP after admission."
    read = session.register_read(chosen.views[0], "sources/manual.json", "pages=1-2", quote)
    for condition in ("external", "when configured"):
        assert "Unverified" in session.record_fact(
            read.view_id,
            subject="Compute nodes",
            setting="VIP",
            condition=condition,
            quote=quote,
            locator=read.read_id,
        )
    second = session.register_read(chosen.views[0], "sources/manual.json", "pages=1", quote)
    report = EvidenceReview.model_validate(
        {
            "units": [
                {
                    "unit_id": 0,
                    "verdict": "supported",
                    "reason": "",
                    "proofs": [
                        {
                            "read_id": second.read_id,
                            "quote": quote,
                            "subject": "Compute nodes",
                            "setting": "VIP",
                            "condition": "after admission",
                            "visual_region": None,
                        }
                    ],
                }
            ]
        }
    )
    reviewed = assess_review(quote, session, report)
    assert reviewed.issues and not reviewed.accepted_units


def test_review_extracts_exact_original_lines_without_retyping_wrapped_rules(kb_dir):
    from openkb.agent.answer_review import EvidenceReview, assess_review
    from openkb.agent.evidence_session import EvidenceSession

    text = (
        "Arbiter stores metadata.\nIt does not store user data; capacity depends\non inode space."
    )
    (kb_dir / "wiki/sources/manual.md").write_text(text)
    chosen = selection(kb_dir)
    session = EvidenceSession(chosen)
    read = session.register_read(chosen.views[0], "sources/manual.md", "original", text)
    report = EvidenceReview.model_validate(
        {
            "units": [
                {
                    "unit_id": 0,
                    "verdict": "supported",
                    "reason": "",
                    "proofs": [
                        {
                            "read_id": read.read_id,
                            "quote": "",
                            "lines": [1, 3],
                            "subject": "Arbiter",
                            "setting": "does not store user data",
                            "condition": "depends on inode space",
                            "visual_region": None,
                        }
                    ],
                }
            ]
        }
    )
    result = assess_review("Arbiter stores metadata; size depends on inode space.", session, report)
    assert result.accepted_units == (0,)
    assert next(iter(session.facts.values()))["quote"] == text
    report.units[0].proofs[0].lines = (1, 4)
    assert not assess_review("Unsupported out-of-range citation.", session, report).accepted_units


def test_real_ntp_review_recovers_blank_line_offset_and_context_only_citations(kb_dir):
    import json
    from pathlib import Path

    from openkb.agent.answer_review import EvidenceReview, assess_review
    from openkb.agent.evidence_session import EvidenceRead, EvidenceSession

    fixture = json.loads((Path(__file__).parent / "fixtures/ntp-review-lines.json").read_text())
    session = EvidenceSession(selection(kb_dir, ("9.4.0",)))
    session.reads = {r["read_id"]: EvidenceRead(**r) for r in fixture["reads"]}
    report = EvidenceReview.model_validate(fixture["review"])
    result = assess_review(fixture["answer"], session, report)
    assert not result.issues and "管理节点 VIP" in result.body and "纳管" in result.body
    # A page-number-only proof cannot certify a configuration value.
    report.units[2].proofs = [report.units[2].proofs[1]]
    result = assess_review(fixture["answer"], session, report)
    assert 2 not in result.accepted_units


def test_real_cpu_review_matches_compatibility_glyphs_without_rewriting_source(kb_dir):
    import json
    from pathlib import Path

    from openkb.agent.answer_review import EvidenceReview, assess_review
    from openkb.agent.evidence_session import EvidenceRead, EvidenceSession

    fixture = json.loads((Path(__file__).parent / "fixtures/cpu-review-lines.json").read_text())
    read = EvidenceRead(**fixture["read"])
    session = EvidenceSession(selection(kb_dir, ("9.4.0",)))
    session.reads = {read.read_id: read}
    report = EvidenceReview.model_validate(fixture["review"])
    result = assess_review(fixture["answer"], session, report)
    assert result.accepted_units == (0,) and not result.issues
    fact = next(iter(session.facts.values()))
    assert "不⼀致" in fact["quote"] and fact["quote"] in read.content
    assert read.digest == fixture["read"]["digest"]
    report.units[0].proofs[0].setting = "unrelated CPU setting"
    assert not assess_review(fixture["answer"], session, report).accepted_units


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_prefix", [False, True])
async def test_truncated_review_retains_only_complete_verified_units(
    kb_dir, monkeypatch, malformed_prefix
):
    import json
    from types import SimpleNamespace

    from agents import Agent

    from openkb.agent.answer_finalization import finalize_answer
    from openkb.agent.evidence_session import EvidenceSession

    source(kb_dir)
    chosen = selection(kb_dir)
    session = EvidenceSession(chosen)
    text = "Compute nodes use management VIP after joining the cluster."
    read = session.register_read(chosen.views[0], "sources/manual.json", "pages=1", text)
    proof = {
        "read_id": read.read_id,
        "quote": text,
        "subject": "Compute nodes",
        "setting": "management VIP",
        "condition": "after joining the cluster",
        "visual_region": None,
    }
    unit = {"unit_id": 0, "verdict": "supported", "reason": "", "proofs": [proof]}
    if malformed_prefix:
        unit["unit_id"] = 1
    raw = '{"units":[' + json.dumps(unit) + ',{"unit_id":1,"verdict":"supp'
    calls = []

    async def model(*args, **kwargs):
        calls.append(1)
        return SimpleNamespace(final_output=raw, raw_responses=[])

    monkeypatch.setattr("agents.Runner.run", model)
    decision, _ = await finalize_answer(
        Agent(name="fixture"),
        "Upstream and unrelated rule?",
        text + "\nAn unrelated rule without a completed review.",
        session,
    )
    assert len(calls) == 1  # No unbounded model retry to repair malformed JSON.
    assert "unrelated rule" not in decision.answer
    if malformed_prefix:
        assert decision.outcome == "insufficient_evidence" and text not in decision.answer
    else:
        assert decision.outcome == "partial" and text in decision.answer
        assert session.review_audit[0]["complete"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("repair_result", ["unchanged", "empty", "invalid_review"])
async def test_only_one_local_repair_then_preserve_supported_conclusions(
    kb_dir, monkeypatch, repair_result
):
    import json
    from types import SimpleNamespace

    from agents import Agent

    from openkb.agent.answer_finalization import finalize_answer
    from openkb.agent.answer_review import EvidenceReview
    from openkb.agent.evidence_session import EvidenceSession

    source(kb_dir)
    chosen = selection(kb_dir)
    session = EvidenceSession(chosen)
    quote = "Compute nodes use management VIP after joining the cluster."
    read = session.register_read(chosen.views[0], "sources/manual.json", "physical page 54", quote)
    good = "Compute nodes use management VIP after joining the cluster."
    bad = "Compute nodes use external NTP when not configured."
    draft = good + "\n" + bad
    calls = []

    async def model(agent, input, **kwargs):
        calls.append(agent.name)
        assert not agent.tools and kwargs["max_turns"] == 1
        if agent.name == "evidence-repair":
            return SimpleNamespace(
                final_output="" if repair_result == "empty" else draft, raw_responses=[]
            )
        if len(calls) == 3 and repair_result == "invalid_review":
            return SimpleNamespace(final_output="", raw_responses=[])
        payload = json.loads(input[0]["content"][0]["text"])
        assert payload["reads"][0]["read_id"] == read.read_id
        report = EvidenceReview.model_validate(
            {
                "units": [
                    {
                        "unit_id": 0,
                        "verdict": "supported",
                        "reason": "",
                        "proofs": [
                            {
                                "read_id": read.read_id,
                                "quote": quote,
                                "subject": "Compute nodes",
                                "setting": "management VIP",
                                "condition": "after joining the cluster",
                                "visual_region": None,
                            }
                        ],
                    },
                    {
                        "unit_id": 1,
                        "verdict": "unsupported",
                        "reason": "Condition borrowed from management nodes",
                        "proofs": [],
                    },
                ]
            }
        )
        return SimpleNamespace(final_output=report, raw_responses=[])

    monkeypatch.setattr("agents.Runner.run", model)
    decision, _ = await finalize_answer(Agent(name="fixture"), "Compute node NTP?", draft, session)
    expected = ["evidence-review", "evidence-repair"]
    if repair_result != "empty":
        expected.append("evidence-review")
    assert calls == expected
    assert decision.outcome == "partial" and good in decision.answer and bad not in decision.answer
    assert session.accepted
