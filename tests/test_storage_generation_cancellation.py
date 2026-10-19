"""Disconnects join the actual SDK and retain the late transport receipt."""

import asyncio
from threading import Event

import pytest
from stream_transport import StreamTransport
from test_storage_deck_generation import deck_knowledge

from ultrakb.application.views import storage_scope
from ultrakb.storage.artifact_store import ArtifactStore
from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.maintenance import Maintenance
from ultrakb.storage.usage_store import UsageStore

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["deck", "skill", "json"])
async def test_generation_disconnect_and_repeated_cancel_wait_for_the_actual_provider(
    tmp_path, monkeypatch, kind
):
    import ultrakb.api_views as views
    from ultrakb.api_helpers import _stream_deck, _stream_skill
    from ultrakb.api_models import DeckRequest, SkillRequest

    root, identity = deck_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    monkeypatch.setattr(views, "view_scope", lambda *args, **kwargs: scope)
    entered, release, disconnected = Event(), Event(), Event()

    def respond(request, index):
        entered.set()
        assert release.wait(8)
        return {"content": "A late provider response with actual usage."}

    class Request:
        async def is_disconnected(self):
            return disconnected.is_set()

    stream = _stream_deck if kind == "deck" else _stream_skill
    model = DeckRequest if kind == "deck" else SkillRequest
    request = model(kb=str(root), name="value", intent="Present value.", view_id="legacy")
    lock = asyncio.Lock()

    async def consume():
        if kind == "json":
            from httpx import ASGITransport, AsyncClient

            import ultrakb.api as api

            monkeypatch.setattr(api, "_resolve_kb", lambda *args: root)
            monkeypatch.delenv("ULTRAKB_API_TOKEN", raising=False)
            async with AsyncClient(
                transport=ASGITransport(app=api.create_app()), base_url="http://test"
            ) as client:
                return await client.post(
                    "/api/v1/deck", json={**request.model_dump(), "stream": False}
                )
        return [frame async for frame in stream(request, root, lock, Request())]

    with StreamTransport(respond) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        pending = asyncio.create_task(consume())
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            assert Maintenance(root, identity).collect(max_items=1).status == "completed"
            disconnected.set()
            if kind == "json":
                pending.cancel()
            await asyncio.sleep(0.15)
            assert not pending.done() and (kind == "json" or lock.locked())
            pending.cancel()
            await asyncio.sleep(0.05)
            pending.cancel()
            assert not pending.done() and (kind == "json" or lock.locked())
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 5)
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        assert len(service.requests) == 1
        assert not lock.locked() and ArtifactStore(root, identity).epoch() == 0
        with storage_stage(root, identity, read_only=True) as db:
            executions = db.connection.execute(
                "SELECT execution_id, state FROM ukb_executions"
            ).fetchall()
        assert len(executions) == 1 and executions[0][1] == "cancelled"
        receipt = UsageStore(root, identity).receipt(execution_id=executions[0][0])
        assert receipt["current"]["requests"] == receipt["current"]["actual_sends"] == 1
        assert receipt["current"]["in_flight_requests"] == 0
