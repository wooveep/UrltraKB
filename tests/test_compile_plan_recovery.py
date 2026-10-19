"""Actual plan request/recovery entry with retained domain inputs and error-only HTTP.

Cached plans are prepared through the storage interface, never returned by a fake
model. A cache miss can only reach a genuine SDK and the local HTTP error service.
"""

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from error_transport import ErrorTransport

from ultrakb.agent.compile_plan import CompilePlanError, request_compile_plan
from ultrakb.agent.page_response import PageResponseError
from ultrakb.llm_execution import (
    ModelBudgetExceeded,
    ModelRequest,
)

pytestmark = pytest.mark.integration

PLAN_DIGEST = "a" * 64


@pytest.fixture
def plan_runtime(tmp_path):
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.config import save_config
    from ultrakb.storage.checkpoint_store import CompileCheckpointStore
    from ultrakb.storage.execution_store import ExecutionStore

    root = tmp_path / "plan-kb"
    initialize_kb(root, seed_environment=False)

    @contextmanager
    def open_runtime(*, max_calls=0, retained_used=0):
        save_config(
            root / ".ultrakb/config.yaml",
            {
                "model": "openai/gpt-4o-mini",
                "model_policy": {"max_calls": max(max_calls, retained_used), "retries": 0},
                "model_capacity": {"max_input_tokens": 65536, "tokenizer_model": "gpt-4o-mini"},
            },
        )
        with ErrorTransport(status=400) as service:
            (root / ".env").write_text(
                f"LLM_API_KEY=offline-plan-key\nOPENAI_API_BASE={service.url}\n"
            )
            context = ExecutionContext()
            with context.begin(root):
                executor = context.executor
                execution = context.storage_execution
                store = CompileCheckpointStore(root, context.storage_identity)

                def checkpoints(conditions=None):
                    return store.for_work(
                        execution.model_budget_id,
                        object_id={"document": "retained-source"},
                        conditions=conditions or {"protocol": "plan-v1"},
                        execution_id=execution.execution_id,
                    )

                requested = []

                def request_plan(correction):
                    requested.append(correction)
                    messages = [{"role": "user", "content": "Plan the retained original."}]
                    if correction is not None:
                        messages.append({"role": "user", "content": correction})
                    return executor.complete(
                        "compile",
                        ModelRequest(
                            messages,
                            operation="compile",
                            stage="concepts-plan",
                            generation_options={"response_format": {"type": "json_object"}},
                        ),
                    ).text

                for index in range(retained_used):
                    try:
                        request_plan("Earlier task attempt")
                    except Exception:
                        pass
                    assert len(service.requests) == index + 1
                requested.clear()
                yield SimpleNamespace(
                    service=service,
                    executor=executor,
                    checkpoints=checkpoints(),
                    requested=requested,
                    request=request_plan,
                    reopen=checkpoints,
                    sends_before=retained_used,
                    budget_used=lambda: ExecutionStore(root, context.storage_identity)
                    .budget(execution.model_budget_id)
                    .used,
                )

    return open_runtime


def receive_plan(runtime, **options):
    options.setdefault("existing", {"concepts": set(), "entities": set()})
    options.setdefault("entity_types", frozenset({"product", "other"}))
    options.setdefault("sanitize", str.lower)
    request_digest = options.pop("request_digest", PLAN_DIGEST)
    with runtime.executor.activate():
        return request_compile_plan(
            runtime.request,
            json.loads,
            checkpoints=runtime.checkpoints,
            request_digest=request_digest,
            **options,
        )


def test_invalid_json_in_retained_plan_is_classified_without_requesting_a_model(plan_runtime):
    with plan_runtime() as runtime:
        runtime.checkpoints.save("plan", PLAN_DIGEST, "{invalid-json")
        with pytest.raises(CompilePlanError) as caught:
            receive_plan(runtime)
        assert caught.value.code == "concept_plan_unparseable"
        assert not runtime.requested and len(runtime.service.requests) == runtime.sends_before
        assert runtime.executor.calls == 0


def test_rejected_cached_plan_persists_its_failure_without_claiming_a_repair(plan_runtime):
    with plan_runtime() as runtime:
        raw = json.dumps({"concepts": 42})
        runtime.checkpoints.save("plan", PLAN_DIGEST, raw)
        with pytest.raises(CompilePlanError) as caught:
            receive_plan(runtime)
        assert caught.value.code == "malformed_plan_items"
        reopened = runtime.reopen()
        assert reopened.failure_code("plan", PLAN_DIGEST) == "malformed_plan_items"
        reopened.require_generation("plan", PLAN_DIGEST)
        assert reopened.load("plan", PLAN_DIGEST) == raw
        assert not runtime.requested and len(runtime.service.requests) == runtime.sends_before
        assert runtime.executor.calls == 0


@pytest.mark.parametrize("retained", [{"concepts": {}}, [], 42])
def test_nontext_cached_plan_is_rejected_at_the_plan_boundary(plan_runtime, retained):
    with plan_runtime() as runtime:
        runtime.checkpoints.save("plan", PLAN_DIGEST, retained)
        with pytest.raises(CompilePlanError) as caught:
            receive_plan(runtime)
        assert caught.value.code == "concept_plan_unparseable"
        assert runtime.checkpoints.failure_code("plan", PLAN_DIGEST) == "concept_plan_unparseable"
        assert not runtime.requested and len(runtime.service.requests) == runtime.sends_before


@pytest.mark.parametrize("repair_used", [False, True])
def test_valid_cached_plan_restores_with_a_spent_task_and_no_model_request(
    plan_runtime, repair_used
):
    with plan_runtime(max_calls=2, retained_used=2) as runtime:
        if repair_used:
            runtime.checkpoints.claim_repair("plan", PLAN_DIGEST)
        raw = json.dumps(
            {
                "concepts": {"create": [{"name": "orion", "evidence_units": [1]}]},
                "entities": {
                    "create": [{"name": "nova", "type": "product", "evidence_units": [1]}]
                },
            }
        )
        runtime.checkpoints.save("plan", PLAN_DIGEST, raw)
        before = (
            runtime.checkpoints.accepted_plan(),
            runtime.checkpoints.failure_code("plan", PLAN_DIGEST),
        )
        plan = receive_plan(runtime, require_evidence=True)
        assert plan.concepts.create == [{"name": "orion", "title": "orion", "evidence_units": [1]}]
        assert plan.entities.create == [
            {"name": "nova", "title": "nova", "type": "product", "evidence_units": [1]}
        ]
        assert (
            runtime.checkpoints.accepted_plan(),
            runtime.checkpoints.failure_code("plan", PLAN_DIGEST),
        ) == before
        assert not runtime.requested and len(runtime.service.requests) == runtime.sends_before
        assert runtime.executor.calls == runtime.budget_used() == 2


@pytest.mark.parametrize(
    ("retained", "options"),
    [
        ({"concepts": {"update": [{"name": "removed-target"}]}}, {}),
        ({"entities": {"create": [{"name": "nova", "type": "planet"}]}}, {}),
        ({"concepts": {"create": [{"name": "orion"}]}}, {"require_evidence": True}),
        ({"concepts": {"create": [{"name": "orion"}, {"name": "ORION"}]}}, {}),
    ],
    ids=["removed-target", "changed-type-policy", "missing-evidence", "conflicting-actions"],
)
def test_cached_plan_is_revalidated_against_current_domain_constraints(
    plan_runtime, retained, options
):
    with plan_runtime() as runtime:
        raw = json.dumps(retained)
        runtime.checkpoints.save("plan", PLAN_DIGEST, raw)
        with pytest.raises(CompilePlanError) as caught:
            receive_plan(runtime, **options)
        assert caught.value.code == "malformed_plan_items"
        assert runtime.checkpoints.failure_code("plan", PLAN_DIGEST) == "malformed_plan_items"
        assert runtime.checkpoints.load("plan", PLAN_DIGEST) == raw
        assert not runtime.requested and len(runtime.service.requests) == runtime.sends_before


def test_cached_plan_still_requires_current_original_reading_validation(plan_runtime):
    with plan_runtime() as runtime:
        runtime.checkpoints.save(
            "plan", PLAN_DIGEST, json.dumps({"concepts": {"update": [{"name": "orion"}]}})
        )
        validated = []

        def validate_reading(plan):
            validated.append(plan.concepts.update)
            return "The current original for concepts/orion.md has not been read."

        with pytest.raises(CompilePlanError) as caught:
            receive_plan(
                runtime,
                existing={"concepts": {"orion"}, "entities": set()},
                validate_updates=validate_reading,
            )
        assert validated == [[{"name": "orion", "title": "orion"}]]
        assert caught.value.code == "page_update_originals_unread"
        assert (
            runtime.checkpoints.failure_code("plan", PLAN_DIGEST) == "page_update_originals_unread"
        )
        assert not runtime.requested and len(runtime.service.requests) == runtime.sends_before


def test_exhausted_plan_repair_refuses_the_real_request_entry_without_overwriting_failure(
    plan_runtime,
):
    with plan_runtime(max_calls=2) as runtime:
        runtime.checkpoints.claim_repair("plan", PLAN_DIGEST)
        runtime.checkpoints.record_failure("plan", PLAN_DIGEST, "malformed_plan_items")
        before = (
            runtime.checkpoints.accepted_plan(),
            runtime.checkpoints.failure_code("plan", PLAN_DIGEST),
        )
        with pytest.raises(PageResponseError) as caught:
            receive_plan(runtime)
        assert caught.value.code == "compile_repair_exhausted"
        assert (
            runtime.checkpoints.accepted_plan(),
            runtime.checkpoints.failure_code("plan", PLAN_DIGEST),
        ) == before
        assert not runtime.requested and len(runtime.service.requests) == runtime.sends_before
        assert runtime.executor.calls == 0


def test_uncached_plan_with_no_task_allowance_stops_before_transport(plan_runtime):
    with plan_runtime(max_calls=2, retained_used=2) as runtime:
        with pytest.raises(ModelBudgetExceeded):
            receive_plan(runtime)
        assert runtime.requested == [None]
        assert len(runtime.service.requests) == runtime.sends_before
        assert runtime.executor.calls == runtime.budget_used() == 2
        assert runtime.checkpoints.accepted_plan() is None


def test_rebuilt_index_identity_does_not_repeat_an_accepted_plan(plan_runtime):
    from ultrakb.agent.planning_capacity import navigation_messages
    from ultrakb.compile_checkpoints import digest

    navigation = [{"title": "Original chapter", "node_id": "0001", "start_index": 1}]
    original = digest(navigation_messages("manual", "old-index-id", navigation))
    rebuilt = digest(navigation_messages("manual", "rebuilt-index-id", navigation))
    assert original != rebuilt
    with plan_runtime(max_calls=1, retained_used=1) as runtime:
        raw = json.dumps({"concepts": {"create": [{"name": "orion", "evidence_units": [1]}]}})
        runtime.checkpoints.save("plan", original, raw)
        before = (
            runtime.checkpoints.accepted_plan(),
            runtime.checkpoints.failure_code("plan", PLAN_DIGEST),
        )
        plan = receive_plan(runtime, request_digest=rebuilt, require_evidence=True)
        assert plan.concepts.create[0]["name"] == "orion"
        assert (
            runtime.checkpoints.accepted_plan(),
            runtime.checkpoints.failure_code("plan", PLAN_DIGEST),
        ) == before
        assert not runtime.requested and len(runtime.service.requests) == runtime.sends_before
        assert runtime.executor.calls == runtime.budget_used() == 1


def test_rebuilt_plan_still_validates_retained_original_locations(plan_runtime):
    with plan_runtime() as runtime:
        raw = json.dumps({"concepts": {"update": [{"name": "orion"}]}})
        runtime.checkpoints.save("plan", PLAN_DIGEST, raw)
        with pytest.raises(CompilePlanError, match="original") as caught:
            receive_plan(
                runtime,
                request_digest="b" * 64,
                existing={"concepts": {"orion"}, "entities": set()},
                validate_updates=lambda plan: "The original selection is no longer available.",
            )
        assert caught.value.code == "page_update_originals_unread"
        assert runtime.checkpoints.failure_code("plan", PLAN_DIGEST) == caught.value.code
        assert runtime.checkpoints.load("plan", PLAN_DIGEST) == raw
        assert not runtime.requested and len(runtime.service.requests) == runtime.sends_before


def test_changed_source_conditions_do_not_resume_an_old_plan(plan_runtime):
    with plan_runtime() as runtime:
        runtime.checkpoints.save("plan", PLAN_DIGEST, json.dumps({"concepts": {}}))
        runtime.checkpoints = runtime.reopen(
            {"protocol": "plan-v1", "source_revision": "changed-original"}
        )
        with pytest.raises(ModelBudgetExceeded):
            receive_plan(runtime, request_digest="b" * 64)
        assert runtime.requested == [None] and len(runtime.service.requests) == runtime.sends_before
