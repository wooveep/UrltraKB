"""Single-page recovery preserves products and counts bounded transport attempts."""

from copy import deepcopy

import pytest

from openkb.agent.document_global_context import freeze_context
from openkb.agent.document_page_preparation import PreparationContext, prepare_planned_pages
from openkb.agent.document_page_sources import accept_locations
from openkb.agent.document_plan import DocumentPlan, OverviewPlan
from openkb.agent.evidence_checkpoints import CompilationCheckpoints
from openkb.processing import RequestLimits
from tests.test_document_markdown_planning import SETTINGS
from tests.test_document_page_resolution import _page, _source


def setup(kb_dir, tmp_path, caller, *, count=1, settings=None):
    settings = settings or {**SETTINGS, "model": "gpt-4o"}
    source, parsed, reader = _source(kb_dir, tmp_path)
    navigation = {
        "nodes": [
            {"id": "auth", "parent": None, "title": "Credentials", "start": 0, "end": 2},
            {"id": "install", "parent": None, "title": "Install", "start": 2, "end": 4},
        ]
    }
    state = {
        "windows": [],
        "pages": [],
        "deferred_suggestions": [],
        "overview_snapshot": None,
        "retained_fragments": [],
        "fragments": {},
        "tasks": {},
    }
    snapshot = freeze_context(
        state,
        navigation,
        source,
        parsed,
        [],
        settings,
        RequestLimits.from_config(settings),
        [],
        "",
        [],
    )
    pages = []
    for i in range(count):
        page = _page([])
        page.key = str(i)
        page.title, page.purpose = f"Prerequisite topic {i}", "Explain access prerequisites"
        pages.append(page)
    plan = DocumentPlan(
        metadata={
            "protocol": "document-plan-v4",
            "recovery_key": "d" * 64,
            "planning_snapshot": snapshot,
        },
        overview=OverviewPlan(text="Saved overview"),
        pages=pages,
    )
    checkpoints = CompilationCheckpoints(kb_dir, source, parsed, settings, None)
    context = PreparationContext(
        source, parsed, navigation, reader, settings, checkpoints, mock_caller=caller
    )
    return plan, context


def test_missing_subject_recovers_one_page_with_no_other_changes(kb_dir, tmp_path):
    calls = []

    def respond(messages, **kwargs):
        calls.append(messages)
        return "Title: New unwanted page\nType: person\nSubject: section:auth; section:missing"

    plan, ctx = setup(kb_dir, tmp_path, respond, count=2)
    plan.pages[1].location_hints = [{"role": "subject", "value": "Install"}]
    result = prepare_planned_pages(plan, ctx)
    assert len(calls) == 1
    assert [row.page.state for row in result] == ["ready", "ready"]
    assert result[0].page.subject_ranges == [[0, 2]]
    assert result[0].page.title == "Prerequisite topic 0" and result[0].page.kind == "concept"
    assert plan.overview.text == "Saved overview"
    assert "Obtain the access token first." in str(result[0].evidence)
    assert any("missing" in note for note in result[0].page.planning_notes)
    again = prepare_planned_pages(plan, ctx)
    assert len(calls) == 1 and all(row.page.state == "ready" for row in again)


def test_empty_then_valid_keeps_prefix_and_persists_raw_before_acceptance(
    kb_dir, tmp_path, monkeypatch
):
    calls = []

    def respond(messages, **kwargs):
        calls.append(messages)
        return "" if len(calls) == 1 else "- Credentials"

    plan, ctx = setup(kb_dir, tmp_path, respond)
    import openkb.agent.document_page_preparation as module

    original = module.accept_locations

    def crash(raw, *args):
        if raw:
            raise RuntimeError("local interruption after saved response")
        return original(raw, *args)

    monkeypatch.setattr(module, "accept_locations", crash)
    with pytest.raises(RuntimeError, match="local interruption"):
        prepare_planned_pages(plan, ctx)
    monkeypatch.setattr(module, "accept_locations", original)
    # Automatic resume keeps the same round; saved responses require no dispatch.
    result = prepare_planned_pages(plan, ctx)
    assert len(calls) == 2 and result[0].page.state == "ready"
    assert calls[0][:2] == calls[1][:2]


def test_bad_responses_have_page_and_document_caps_and_continue_is_explicit(kb_dir, tmp_path):
    calls = []

    def respond(messages, **kwargs):
        calls.append(messages)
        return "Not a location"

    plan, ctx = setup(kb_dir, tmp_path, respond, count=4)
    result = prepare_planned_pages(plan, ctx)
    assert len(calls) == 6 and all(row.page.state == "skipped" for row in result)
    assert len(plan.metadata["page_preparation"]["attempts"]) == 6
    prepare_planned_pages(plan, ctx)
    assert len(calls) == 6
    ctx.retry_skipped = True
    prepare_planned_pages(plan, ctx)
    assert len(calls) == 12
    assert plan.metadata["page_preparation"]["round"] == 2


def test_explicit_negative_and_large_declared_scope_do_not_repeat(kb_dir, tmp_path):
    calls = []

    def respond(messages, **kwargs):
        calls.append(messages)
        return "None"

    plan, ctx = setup(kb_dir, tmp_path, respond)
    result = prepare_planned_pages(plan, ctx)
    assert result[0].reason == "page_sources_no_support" and len(calls) == 1
    assert plan.pages[0].state == "skipped"


def test_over_budget_and_external_reference_do_not_trigger_location(kb_dir, tmp_path, monkeypatch):
    import openkb.agent.document_page_preparation as module

    plan, ctx = setup(kb_dir, tmp_path, lambda *a, **kw: pytest.fail("unexpected model call"))
    plan.pages[0].subject_ranges = [[0, 6]]
    plan.pages[0].planning_notes = ["Refer to external manual; not imported"]
    monkeypatch.setattr(module, "preparation_max_chars", lambda limits: 1)
    result = prepare_planned_pages(plan, ctx)
    assert result[0].reason == "page_evidence_budget_limited"
    assert result[0].page.subject_ranges == [[0, 6]]


@pytest.mark.parametrize(
    "text",
    [
        "- Credentials",
        "Subject: section:auth (Credentials)",
        "- `section:auth` — Credentials",
        "- `section:auth` Credentials",
        '{"Selection": ["section:auth", "section:unknown"],}',
        "| Selection |\n|---|\n| Credentials |",
    ],
)
def test_shared_location_shapes_and_no_numeric_authority(kb_dir, tmp_path, text):
    plan, ctx = setup(kb_dir, tmp_path, None)
    selection = accept_locations(text, plan.metadata["planning_snapshot"]["nodes"], ctx.parsed)
    assert selection.ranges == [[0, 2]]
    if "unknown" not in text:
        assert not selection.unresolved
    assert not accept_locations(
        '{"source_id":"elsewhere","subject_ranges":[0,2]}',
        plan.metadata["planning_snapshot"]["nodes"],
        ctx.parsed,
    ).ranges


def test_transport_retries_are_charged_before_provider_call(kb_dir, tmp_path, monkeypatch):
    import litellm
    from litellm import ModelResponse

    from openkb.processing import ProcessingIncomplete

    plan, ctx = setup(kb_dir, tmp_path, None)
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        assert kwargs["num_retries"] == kwargs["max_retries"] == 0
        if len(calls) < 3:
            raise litellm.ServiceUnavailableError(
                "temporary", llm_provider="openai", model="gpt-4o"
            )
        return ModelResponse(
            choices=[{"message": {"content": "Subject: Credentials"}, "finish_reason": "stop"}]
        )

    monkeypatch.setattr(litellm, "completion", completion)
    # Exercise ExecutionBudget's actual retry loop without its network backoff.
    monkeypatch.setattr("openkb.processing.time.sleep", lambda seconds: None)
    try:
        result = prepare_planned_pages(plan, ctx)
    except ProcessingIncomplete as exc:
        pytest.fail(str(exc))
    assert len(calls) == 3 and result[0].page.state == "ready"
    assert len(plan.metadata["page_preparation"]["attempts"]) == 3


def test_reconstructed_plan_reuses_accepted_location_response(kb_dir, tmp_path):
    calls = []

    def respond(messages, **kwargs):
        calls.append(messages)
        return "Subject: Credentials"

    plan, ctx = setup(kb_dir, tmp_path, respond)
    original = deepcopy(plan)
    assert prepare_planned_pages(plan, ctx)[0].page.state == "ready"
    restored = prepare_planned_pages(original, ctx)
    assert len(calls) == 1 and restored[0].page.subject_ranges == [[0, 2]]
    assert original.overview.text == "Saved overview"


def test_coarse_navigation_keeps_valid_leaf_during_parent_drill(kb_dir, tmp_path):
    import json

    replies = iter(["- Credentials\n- section:parent", "- section:install"])
    plan, ctx = setup(kb_dir, tmp_path, lambda *a, **kw: next(replies))
    snapshot = plan.metadata["planning_snapshot"]
    parent = {
        **snapshot["nodes"][1],
        "section_key": "section:parent",
        "title": "Procedure",
        "heading_path": ["Procedure"],
        "original_ranges": [[2, 6]],
    }
    snapshot["nodes"][1]["parent"] = "section:parent"
    snapshot["nodes"].insert(1, parent)
    common = json.loads(snapshot["context_json"])
    common["topics"] = snapshot["nodes"][:2]
    common["projection"]["omitted_nodes"] = 1
    snapshot["context_json"] = json.dumps(common)
    result = prepare_planned_pages(plan, ctx)
    assert result[0].page.state == "ready"
    assert result[0].page.subject_ranges == [[0, 2], [2, 4]]
    assert "Obtain the access token first." in str(result[0].evidence)


def test_coarsest_flat_directory_accepts_real_leaf_without_key_error(kb_dir, tmp_path):
    import json

    plan, ctx = setup(kb_dir, tmp_path, lambda *a, **kw: "Credentials")
    snapshot = plan.metadata["planning_snapshot"]
    common = json.loads(snapshot["context_json"])
    common["topics"] = [
        {"from_section_key": "section:auth", "through_section_key": "section:install"}
    ]
    common["projection"]["omitted_nodes"] = 2
    snapshot["context_json"] = json.dumps(common)
    assert prepare_planned_pages(plan, ctx)[0].page.subject_ranges == [[0, 2]]


def test_service_failure_after_bad_content_survives_limit_and_first_continue(
    kb_dir, tmp_path, monkeypatch
):
    import litellm
    from litellm import ModelResponse

    from openkb.processing import ProcessingIncomplete

    plan, ctx = setup(kb_dir, tmp_path, None)
    calls = []
    recovered = False

    def completion(**kwargs):
        calls.append(kwargs)
        if len(calls) > 1 and not recovered:
            raise litellm.ServiceUnavailableError(
                "temporary", llm_provider="openai", model="gpt-4o"
            )
        return ModelResponse(
            choices=[
                {
                    "message": {"content": "Credentials" if recovered else "Unknown"},
                    "finish_reason": "stop",
                }
            ]
        )

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr("openkb.processing.time.sleep", lambda seconds: None)
    with pytest.raises(ProcessingIncomplete, match="provider_temporarily_unavailable"):
        prepare_planned_pages(plan, ctx)
    assert len(calls) == 3
    recovered = True
    ctx.retry_skipped = True
    result = prepare_planned_pages(plan, ctx)
    assert len(calls) == 4 and result[0].page.state == "ready"
    assert plan.metadata["page_preparation"]["round"] == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("accepted_response", {}),
        ("details", "bad"),
        ("responses", 42),
        ("finished_round", 0),
    ],
)
def test_corrupt_recovery_is_rejected_at_boundary(kb_dir, tmp_path, field, value):
    from openkb.agent.document_page_preparation import _validate_state

    plan, ctx = setup(kb_dir, tmp_path, None)
    state = {
        "protocol": "page-preparation-v1",
        "round": 1,
        "status": "running",
        "pages": {"0": {"identity": "saved", field: value}},
        "attempts": [],
        "usage": [],
    }
    with pytest.raises(ValueError):
        _validate_state(state, plan.metadata["planning_snapshot"], {"0"})


def test_inline_subject_is_prepared_without_location_call(kb_dir, tmp_path):
    from openkb.agent.document_planning_response import accept_pages

    plan, ctx = setup(kb_dir, tmp_path, lambda *a, **kw: pytest.fail("already located"))
    result = accept_pages(
        "## Concepts\n### Create\n- Access — Purpose: Reusable prerequisite。Subject: Credentials",
        navigation=plan.metadata["planning_snapshot"]["nodes"],
        target=[[0, 6]],
        parsed=ctx.parsed,
        entity_types=[],
        existing_targets=set(),
    )
    plan.pages = result.pages
    assert plan.pages[0].purpose == "Reusable prerequisite。"
    assert prepare_planned_pages(plan, ctx)[0].page.subject_ranges == [[0, 2]]
