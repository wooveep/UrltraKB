"""Cancelled HTTP artifact reads settle their real GC wait before returning."""

import asyncio
from threading import Event

import httpx
import pytest
from artifact_fixtures import publish_artifacts


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route, extra",
    [
        ("/api/v1/output", {"path": "output/decks/demo/index.html"}),
        ("/api/v1/deck/demo", {}),
        ("/api/v1/skill/demo/archive", {}),
        ("/api/v1/deck", {}),
        ("/api/v1/skill", {}),
    ],
)
async def test_http_artifact_read_cancels_and_joins_its_storage_wait(
    kb_dir, monkeypatch, route, extra
):
    from ultrakb.api import create_app
    from ultrakb.application.views import published_scope
    from ultrakb.config import register_kb_alias
    from ultrakb.locks import _Lease
    from ultrakb.storage.coordination import try_reclamation

    output = kb_dir / "output"
    deck = output / "decks/demo/index.html"
    skill = output / "skills/demo/SKILL.md"
    for path, body in ((deck, "<html>Accepted deck.</html>"), (skill, "# Accepted skill")):
        path.parent.mkdir(parents=True)
        path.write_text(body)
    publish_artifacts(kb_dir, output, sync=True)
    register_kb_alias("artifact-cancel", kb_dir)
    monkeypatch.setenv("ULTRAKB_API_TOKEN", "artifact-cancel-token")
    identity = published_scope(kb_dir).knowledge.identity
    entered, settled = Event(), Event()
    watched = set()
    acquire, release = _Lease.try_acquire, _Lease.release

    def observe_acquire(lease):
        if lease.path == kb_dir / ".ultrakb/locks/reclaim.lock":
            watched.add(id(lease))
            entered.set()
        return acquire(lease)

    def observe_release(lease):
        try:
            return release(lease)
        finally:
            if id(lease) in watched:
                settled.set()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        with try_reclamation(kb_dir, identity) as exclusive:
            assert exclusive is not None
            monkeypatch.setattr(_Lease, "try_acquire", observe_acquire)
            monkeypatch.setattr(_Lease, "release", observe_release)
            request = asyncio.create_task(
                client.get(
                    route,
                    params={"kb": "artifact-cancel", **extra},
                    headers={"Authorization": "Bearer artifact-cancel-token"},
                )
            )
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                assert not settled.is_set()
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(request, 3)
                assert settled.is_set(), "The cancelled storage worker is still waiting on GC"
            finally:
                if not request.done():
                    request.cancel()
        assert await asyncio.to_thread(settled.wait, 5)
