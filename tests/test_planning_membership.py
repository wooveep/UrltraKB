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


@pytest.mark.parametrize("key_method", ["previous_plan_key", "preceding_plan_key"])
@pytest.mark.parametrize("valid", [True, False])
def test_prior_functional_plan_is_revalidated_before_cache_bridge(
    kb_dir, monkeypatch, valid, key_method
):
    from types import SimpleNamespace

    from openkb.agent import compiler
    from openkb.agent.evidence_checkpoints import CompilationCheckpoints
    from openkb.config import DEFAULT_CONFIG
    from openkb.processing import RequestLimits
    from openkb.schema import get_agents_md

    settings = {
        **DEFAULT_CONFIG,
        "model": "openai/offline-test",
        "processing": {
            **DEFAULT_CONFIG["processing"],
            "context_tokens": 32768,
            "max_context_tokens": 32768,
            "output_tokens": 4096,
            "max_output_tokens": 4096,
        },
    }
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
    dependencies = {"catalog_window": "", "schema": request["schema"]}
    previous = getattr(cp, key_method)(plan.PLAN_SYSTEM, request, dependencies=dependencies)
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
        assert plan.plan_topics(*args, bundle=None, on_event=lambda event: None) == []
        assert cp.load(current) is None

    changed = {**request, "existing_pages": "concepts/another-page: Another page"}
    assert (
        cp.load(getattr(cp, key_method)(plan.PLAN_SYSTEM, changed, dependencies=dependencies))
        is None
    )


@pytest.mark.parametrize("removed_member", [False, True])
@pytest.mark.parametrize("changed_context", [False, True])
def test_added_topics_preserve_completed_windows_after_ocr_refresh(
    kb_dir, tmp_path, monkeypatch, changed_context, removed_member
):
    from openkb.agent import compiler
    from openkb.agent.evidence_checkpoints import CompilationCheckpoints
    from openkb.config import DEFAULT_CONFIG
    from openkb.evidence import ParseStore
    from openkb.inputs import prepared_input
    from openkb.parsing import parse_document
    from openkb.processing import RequestLimits
    from openkb.sources import SourceStore

    path = tmp_path / "source.txt"
    path.write_text("Saved original")
    with prepared_input(path) as ready:
        source = SourceStore(kb_dir).intake(ready)
    parsed = parse_document(kb_dir, source)
    settings = {
        **DEFAULT_CONFIG,
        "model": "openai/offline-test",
        "processing": {
            **DEFAULT_CONFIG["processing"],
            "context_tokens": 32768,
            "max_context_tokens": 32768,
            "output_tokens": 4096,
            "max_output_tokens": 4096,
        },
    }
    limits = RequestLimits.from_config(settings)
    calls = []

    def request(model, messages, stage, **kwargs):
        payload = json.loads(messages[-1]["content"])
        calls.append(list(payload["topic_labels"].values()))
        return json.dumps(
            {
                "topics": [
                    {
                        "name": calls[-1][0].lower(),
                        "title": calls[-1][0],
                        "kind": "concept",
                        "members": payload["topics"],
                    }
                ]
            }
        )

    monkeypatch.setattr(compiler, "_llm_call", request)
    monkeypatch.setattr(plan, "MAX_PLAN_TOPICS", 2)
    cp = CompilationCheckpoints(kb_dir, source, parsed, settings, None)
    plan.plan_topics(
        ["Bravo", "Charlie", "Delta", "Echo"],
        kb_dir,
        settings,
        limits,
        cp,
        bundle=None,
        on_event=lambda e: None,
    )
    assert len(calls) == 2
    # A quality-only OCR update changes the parse ID but no planning input.
    from openkb.evidence import BlockDraft

    updated = ParseStore(kb_dir).save(
        source,
        parsed.profile,
        [BlockDraft("Saved original", "paragraph", parsed.blocks[0].location)],
        quality=[{"status": "verified", "reason": "ocr_notice_updated"}],
    )
    assert updated.id != parsed.id
    if changed_context:
        monkeypatch.setattr(
            plan, "_existing_window", lambda *args: "concepts/existing: Existing page"
        )
    resumed = CompilationCheckpoints(kb_dir, source, updated, settings, None)
    calls.clear()
    topics = ["Alpha", "Bravo", "Charlie", "Delta"] + ([] if removed_member else ["Echo"])
    result = plan.plan_topics(
        topics,
        kb_dir,
        settings,
        limits,
        resumed,
        bundle=None,
        on_event=lambda e: None,
        resume=True,
    )
    assert sorted(m for g in result for m in g["members"]) == topics
    if changed_context:
        assert sorted(t for batch in calls for t in batch) == topics
    else:
        assert calls == [["Alpha", "Delta"]] if removed_member else calls == [["Alpha"]]
