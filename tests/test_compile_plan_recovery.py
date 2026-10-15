"""Actual plan request/recovery entry with retained domain inputs and error-only HTTP.

Cached plans are prepared through the storage interface, never returned by a fake
model. A cache miss can only reach a genuine SDK and the local HTTP error service.
"""

import json
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from error_transport import ErrorTransport

from ultrakb.agent.compile_plan import CompilePlanError, request_compile_plan
from ultrakb.agent.page_response import PageResponseError
from ultrakb.compile_checkpoints import CompileCheckpoints
from ultrakb.config import LlmCredentialBundle
from ultrakb.llm_execution import (
    CompletionExecutor,
    ModelBudgetExceeded,
    ModelCallPolicy,
    ModelRequest,
    RoleBindings,
)
from ultrakb.llm_usage import UsageScope, read_requests
from ultrakb.locks import atomic_write_json
from ultrakb.model_task import TaskJournal, read_task, task_path

pytestmark = pytest.mark.integration

PLAN_DIGEST = "a" * 64


@pytest.fixture
def plan_runtime(kb_dir):
    @contextmanager
    def open_runtime(*, max_calls=0, retained_used=0):
        with ErrorTransport(status=400) as service:
            executor = CompletionExecutor(
                RoleBindings(
                    "openai/gpt-4o-mini",
                    LlmCredentialBundle(
                        api_key="offline-plan-key", base_url=service.url, timeout=1
                    ),
                ),
                ModelCallPolicy(max_calls=max_calls, retries=0),
                scope=UsageScope(kb_dir, uuid4().hex),
            )
            journal = TaskJournal(kb_dir, executor)
            if retained_used:
                # This is retained domain state, not a simulated provider receipt.
                retained = journal.record.model_copy(
                    update={"used": retained_used, "stages": {"compile": retained_used}}
                )
                atomic_write_json(task_path(kb_dir, retained.task_id), retained.model_dump())
                executor = CompletionExecutor(
                    executor.bindings, executor.policy, scope=executor.scope
                )
                journal = TaskJournal(kb_dir, executor, resume=retained.task_id)
            checkpoints = CompileCheckpoints(
                kb_dir, journal.record, {"document": "retained-source"}, {"protocol": "plan-v1"}
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

            yield SimpleNamespace(
                service=service,
                executor=executor,
                journal=journal,
                checkpoints=checkpoints,
                requested=requested,
                request=request_plan,
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
        assert not runtime.requested and not runtime.service.requests
        assert runtime.executor.calls == 0


def test_rejected_cached_plan_persists_its_failure_without_claiming_a_repair(plan_runtime, kb_dir):
    with plan_runtime() as runtime:
        raw = json.dumps({"concepts": 42})
        runtime.checkpoints.save("plan", PLAN_DIGEST, raw)
        with pytest.raises(CompilePlanError) as caught:
            receive_plan(runtime)
        assert caught.value.code == "malformed_plan_items"
        reopened = CompileCheckpoints(
            kb_dir,
            runtime.journal.record,
            {"document": "retained-source"},
            {"protocol": "plan-v1"},
        )
        assert reopened.failure_code("plan", PLAN_DIGEST) == "malformed_plan_items"
        reopened.require_generation("plan", PLAN_DIGEST)
        assert reopened.load("plan", PLAN_DIGEST) == raw
        assert not runtime.requested and not runtime.service.requests
        assert runtime.executor.calls == 0


@pytest.mark.parametrize("retained", [{"concepts": {}}, [], 42])
def test_nontext_cached_plan_is_rejected_at_the_plan_boundary(plan_runtime, retained):
    with plan_runtime() as runtime:
        runtime.checkpoints.save("plan", PLAN_DIGEST, retained)
        with pytest.raises(CompilePlanError) as caught:
            receive_plan(runtime)
        assert caught.value.code == "concept_plan_unparseable"
        assert runtime.checkpoints.failure_code("plan", PLAN_DIGEST) == "concept_plan_unparseable"
        assert not runtime.requested and not runtime.service.requests


@pytest.mark.parametrize("repair_used", [False, True])
def test_valid_cached_plan_restores_with_a_spent_task_and_no_model_request(
    plan_runtime, kb_dir, repair_used
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
        before = runtime.checkpoints.path.read_bytes()
        plan = receive_plan(runtime, require_evidence=True)
        assert plan.concepts.create == [{"name": "orion", "title": "orion", "evidence_units": [1]}]
        assert plan.entities.create == [
            {"name": "nova", "title": "nova", "type": "product", "evidence_units": [1]}
        ]
        assert runtime.checkpoints.path.read_bytes() == before
        assert not runtime.requested and not runtime.service.requests
        assert runtime.executor.calls == read_task(kb_dir, runtime.journal.record.task_id).used == 2
        assert not read_requests(kb_dir)


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
        assert not runtime.requested and not runtime.service.requests


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
        assert not runtime.requested and not runtime.service.requests


def test_exhausted_plan_repair_refuses_the_real_request_entry_without_overwriting_failure(
    plan_runtime,
):
    with plan_runtime(max_calls=2) as runtime:
        runtime.checkpoints.claim_repair("plan", PLAN_DIGEST)
        runtime.checkpoints.record_failure("plan", PLAN_DIGEST, "malformed_plan_items")
        before = runtime.checkpoints.path.read_bytes()
        with pytest.raises(PageResponseError) as caught:
            receive_plan(runtime)
        assert caught.value.code == "compile_repair_exhausted"
        assert runtime.checkpoints.path.read_bytes() == before
        assert not runtime.requested and not runtime.service.requests
        assert runtime.executor.calls == 0


def test_uncached_plan_with_no_task_allowance_stops_before_transport(plan_runtime, kb_dir):
    with plan_runtime(max_calls=2, retained_used=2) as runtime:
        with pytest.raises(ModelBudgetExceeded):
            receive_plan(runtime)
        assert runtime.requested == [None]
        assert not runtime.service.requests
        assert runtime.executor.calls == read_task(kb_dir, runtime.journal.record.task_id).used == 2
        assert not runtime.checkpoints.path.exists()
        assert not read_requests(kb_dir)
