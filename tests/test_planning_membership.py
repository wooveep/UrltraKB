"""Planning identities are separate from labels and existing-page identities."""

import json

import pytest

from openkb.agent import evidence_plan as plan
from openkb.processing import ProcessingIncomplete


def row(members):
    return {"topics": [{"name": "task", "title": "Task", "kind": "concept", "members": members}]}


def test_wire_members_are_short_ids_and_decode_exact_unicode_labels():
    topics = [" Task Ａ ", "配置 x.org："]
    request = {"stage": "planning", "topics": topics, "existing_pages": "concepts/task: Task"}
    wire = json.loads(plan.messages(plan.PLAN_SYSTEM, request)[-1]["content"])
    assert wire["topics"] == ["t1", "t2"]
    assert wire["topic_labels"] == dict(zip(wire["topics"], topics))
    decoded = plan.decode_members(row(["t1", "t2"]), topics)
    assert plan._validate(decoded, topics, [])[0]["members"] == topics
    assert request["topics"] == topics


@pytest.mark.parametrize("members", [["t1", "concepts/task"], ["t1", "Task"], ["t2"]])
def test_old_page_members_or_unknown_ids_never_enter_the_plan(members):
    with pytest.raises(ProcessingIncomplete, match="topic_coverage_incomplete"):
        plan.decode_members(row(members), ["Task"])


@pytest.mark.parametrize("members", [["t1", "t1"], []])
def test_short_ids_do_not_relax_duplicate_or_missing_member_checks(members):
    with pytest.raises(ProcessingIncomplete):
        plan._validate(plan.decode_members(row(members), ["Task"]), ["Task"], [])


@pytest.mark.parametrize("valid", [True, False])
def test_prior_functional_plan_is_revalidated_before_cache_bridge(kb_dir, monkeypatch, valid):
    from types import SimpleNamespace

    from openkb.agent import compiler
    from openkb.agent.evidence_checkpoints import CompilationCheckpoints
    from openkb.config import DEFAULT_CONFIG
    from openkb.knowledge_commit import wiki_version
    from openkb.processing import RequestLimits
    from openkb.schema import get_agents_md

    settings = {**DEFAULT_CONFIG, "model": "openai/offline-test"}
    cp = CompilationCheckpoints(
        kb_dir,
        SimpleNamespace(source_id="a" * 32, id="b" * 64),
        SimpleNamespace(id="c" * 64),
        settings,
        None,
    )
    monkeypatch.setattr(plan, "_existing_window", lambda *args: "")
    request = {
        "stage": "planning",
        "topics": ["Task"],
        "entity_types": settings["entity_types"],
        "schema": get_agents_md(kb_dir / "wiki"),
        "existing_pages": "",
    }
    dependencies = wiki_version(kb_dir)
    previous = cp.previous_plan_key(plan.PLAN_SYSTEM, request, dependencies=dependencies)
    current = cp.key(plan.PLAN_SYSTEM, request, dependencies=dependencies)
    assert previous != current
    cp.save(previous, row(["Task"] if valid else ["Task", "Old page"]))

    def unexpected(*args, **kwargs):
        raise AssertionError("A persisted response must be validated without a new request")

    monkeypatch.setattr(compiler, "_llm_call", unexpected)
    args = (["Task"], kb_dir, settings, RequestLimits.from_config(settings), cp)
    if valid:
        result = plan.plan_topics(*args, bundle=None, on_event=lambda event: None)
        assert result[0]["members"] == ["Task"]
        assert cp.load(current) == row(["Task"])
    else:
        with pytest.raises(ProcessingIncomplete, match="topic_coverage_incomplete"):
            plan.plan_topics(*args, bundle=None, on_event=lambda event: None)
        assert cp.load(current) is None

    changed = {**request, "existing_pages": "concepts/another-page: Another page"}
    assert (
        cp.load(cp.previous_plan_key(plan.PLAN_SYSTEM, changed, dependencies=dependencies)) is None
    )
