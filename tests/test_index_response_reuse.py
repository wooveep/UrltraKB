"""Actual SDK responses are retained once across concurrent callers and restart."""

import asyncio
from threading import Event

import litellm
import pytest
from stream_transport import StreamTransport

from ultrakb.index_llm import PageIndexLLM
from ultrakb.llm_execution import CompletionExecutor, LlmCredentialBundle, RoleBindings
from ultrakb.llm_usage import UsageScope
from ultrakb.storage.checkpoint_store import CompileCheckpointStore
from ultrakb.storage.database import open_database
from ultrakb.storage.execution_store import ExecutionStore
from ultrakb.storage.index_checkpoints import StoredIndexStages
from ultrakb.storage.model_usage import StoredModelUsage

pytestmark = pytest.mark.integration


@pytest.fixture
def adapter(kb_dir):
    with open_database(kb_dir, read_only=True) as db:
        identity = db.identity
    executions, retained = ExecutionStore(kb_dir, identity), {}

    def create(service):
        executor = CompletionExecutor(
            RoleBindings(
                "openai/gpt-4o-mini",
                LlmCredentialBundle(api_key="offline-index-key", base_url=service.url),
                model_table={},
            )
        )
        if executor.fingerprint not in retained:
            retained[executor.fingerprint] = executions.begin(
                "model_task",
                fingerprint=executor.fingerprint,
                max_calls=None,
            )
        execution = retained[executor.fingerprint]
        observer = StoredModelUsage(kb_dir, identity, execution)
        executor.scope = UsageScope(kb_dir, execution.execution_id, storage=observer)
        client = PageIndexLLM(executor)
        work = CompileCheckpointStore(kb_dir, identity).for_work(
            execution.model_budget_id,
            object_id={"document": "index-response-fixture"},
            conditions={"protocol": 1},
            execution_id=execution.execution_id,
        )
        client.checkpoints = StoredIndexStages(work)
        return client

    yield create
    for execution in retained.values():
        executions.finish(execution.execution_id, state="completed", work_settled=True)


def test_response_reuse_is_not_limited_to_one_read_per_execution(adapter):
    with StreamTransport([{"content": '{"answer":"yes"}'}]) as service:
        client = adapter(service)
        prompt = [{"role": "user", "content": "The exact retained request"}]
        first = client.complete(prompt, stage="verification")
        assert client.complete(prompt, stage="verification") == first
        assert len(service.requests) == 1
        requests = client.executor.scope.storage.usage.requests(
            execution_id=client.executor.scope.execution_id
        ).records
        assert len(requests) == 1 and requests[0].send_status == "sent"


@pytest.mark.asyncio
async def test_concurrent_identical_requests_share_the_in_flight_response(adapter):
    with StreamTransport([{"content": '{"answer":"yes"}'}]) as service:
        client = adapter(service)
        prompt = [{"role": "user", "content": "Same heading and source context"}]
        results = await asyncio.gather(
            *(client.acomplete(prompt, stage="verification") for _ in range(3))
        )
        assert results[0] == results[1] == results[2]
        assert len(service.requests) == 1


def test_delivered_response_is_reusable_by_a_new_adapter(adapter):
    with StreamTransport([{"content": '{"answer":"yes"}'}]) as service:
        prompt = [{"role": "user", "content": "Persisted paid response"}]
        first = adapter(service).complete(prompt, stage="verification")
        assert adapter(service).complete(prompt, stage="verification") == first
        assert len(service.requests) == 1


@pytest.mark.asyncio
async def test_cancelling_a_waiter_does_not_cancel_the_shared_request(adapter):
    release = Event()
    with StreamTransport([{"content": '{"answer":"yes"}'}], gate=release) as service:
        client = adapter(service)
        prompt = [{"role": "user", "content": "Retain the owner"}]
        owner = asyncio.create_task(client.acomplete(prompt, stage="verification"))
        try:
            assert await asyncio.to_thread(service.wait_for_requests, 1)
            assert client.retained_response(prompt, stage="verification") is None
            waiter = asyncio.create_task(client.acomplete(prompt, stage="verification"))
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert not owner.done()
        finally:
            release.set()
        result = await owner
        assert await client.acomplete(prompt, stage="verification") == result
        assert len(service.requests) == 1


@pytest.mark.asyncio
async def test_sync_and_async_callers_share_one_request(adapter):
    release = Event()
    with StreamTransport([{"content": '{"answer":"yes"}'}], gate=release) as service:
        client = adapter(service)
        prompt = [{"role": "user", "content": "Mixed callers"}]
        owner = asyncio.create_task(client.acomplete(prompt, stage="verification"))
        try:
            assert await asyncio.to_thread(service.wait_for_requests, 1)
            waiter = asyncio.create_task(
                asyncio.to_thread(client.complete, prompt, stage="verification")
            )
            await asyncio.sleep(0)
        finally:
            release.set()
        assert await waiter == await owner
        assert len(service.requests) == 1


def test_an_unchanged_failed_request_is_not_sent_again(adapter):
    from ultrakb.mutation import RecoveryRequired

    response = {
        "error": {"message": "deterministic response failure", "type": "invalid_request_error"}
    }
    with StreamTransport([response], statuses=[400]) as service:
        client = adapter(service)
        for _ in range(2):
            with pytest.raises((litellm.BadRequestError, RecoveryRequired)):
                client.complete([{"role": "user", "content": "unchanged"}], stage="verification")
        assert len(service.requests) == 1
