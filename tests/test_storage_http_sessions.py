"""Authenticated HTTP conversation pages use the same retained originals as Qt."""

import asyncio
from threading import Event, Thread

import httpx
import pytest
from fastapi.testclient import TestClient
from test_storage_questions import question_knowledge

from ultrakb.application.views import storage_scope
from ultrakb.storage.session_store import SessionStore


def test_http_session_pages_originals_and_rejects_stale_delete(tmp_path, monkeypatch):
    from ultrakb import api
    from ultrakb.api import create_app
    from ultrakb.application import views
    from ultrakb.config import register_kb_alias

    root, identity, _ = question_knowledge(tmp_path)
    # Public discovery stays on the old factory until the joint activation gate.
    monkeypatch.setattr(api, "_resolve_kb", lambda name: root)
    store = SessionStore(root, identity)
    for name in ("http-a", "http-b", "http-c"):
        store.create(name, model="gpt-4o-mini", language="en")
    for ordinal in range(70):
        store.append_exchange("http-a", f"turn-{ordinal}", f"Q{ordinal}", f"A{ordinal}")
    monkeypatch.setattr(
        views,
        "view_scope",
        lambda *args, **kwargs: storage_scope(root, identity, cancelled=kwargs.get("cancelled")),
    )
    monkeypatch.setattr("ultrakb.api_views.view_scope", views.view_scope)
    monkeypatch.setenv("ULTRAKB_API_TOKEN", "offline-session-token")
    register_kb_alias("native-sessions", root)
    headers = {"Authorization": "Bearer offline-session-token"}
    base = {"kb": "native-sessions", "view_id": "legacy"}
    with TestClient(create_app()) as client:
        url = "/api/v1/chat/sessions"
        assert client.post(url, json=base).status_code == 401
        first = client.post(url, json={**base, "limit": 2}, headers=headers)
        assert first.status_code == 200, first.text
        assert [item["id"] for item in first.json()["sessions"]] == ["http-a", "http-c"]
        more = client.post(
            url, json={**base, "limit": 2, "after": first.json()["next_after"]}, headers=headers
        )
        assert [item["id"] for item in more.json()["sessions"]] == ["http-b"]
        assert more.json()["next_after"] is None
        load = url + "/load"
        response = client.post(
            load, json={**base, "session_id": "http-a", "limit": 5}, headers=headers
        )
        assert response.status_code == 200, response.text
        history = response.json()
        assert history["turn_count"] == 70 and history["start_ordinal"] == 65
        assert history["user_turns"] == [f"Q{i}" for i in range(65, 70)]
        assert history["assistant_texts"] == [f"A{i}" for i in range(65, 70)]
        older = client.post(
            load,
            json={**base, "session_id": "http-a", "limit": 5, "before": history["older_before"]},
            headers=headers,
        )
        assert older.json()["assistant_texts"] == [f"A{i}" for i in range(60, 65)]
        store.append_exchange("http-a", "later", "Later question.", "Later answer.")
        delete = {**base, "session_id": "http-a", "version": history["version"]}
        conflict = client.post(url + "/delete", json=delete, headers=headers)
        assert conflict.status_code == 409, conflict.text
        missing = client.post(
            "/api/v1/chat",
            json={**base, "session_id": "missing", "message": "Question.", "stream": False},
            headers=headers,
        )
        assert missing.status_code == 404, missing.text
        latest = client.post(load, json={**base, "session_id": "http-a"}, headers=headers).json()
        deleted = client.post(
            url + "/delete", json={**delete, "version": latest["version"]}, headers=headers
        )
        assert deleted.status_code == 200, deleted.text
        assert (
            client.post(load, json={**base, "session_id": "http-a"}, headers=headers).status_code
            == 404
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["", "/load"])
async def test_http_cancelled_history_read_settles_while_gc_still_holds_its_lock(
    tmp_path, monkeypatch, endpoint
):
    from ultrakb import api
    from ultrakb.storage.coordination import try_reclamation

    root, identity, _ = question_knowledge(tmp_path)
    store = SessionStore(root, identity)
    store.create("busy-http", model="gpt-4o-mini", language="en")
    monkeypatch.setattr(api, "_resolve_kb", lambda name: root)
    monkeypatch.setenv("ULTRAKB_API_TOKEN", "offline-read-token")

    monkeypatch.setattr(
        "ultrakb.api_views.view_scope",
        lambda *args, **kwargs: storage_scope(root, identity, cancelled=kwargs.get("cancelled")),
    )
    transport = httpx.ASGITransport(app=api.create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        with try_reclamation(root, identity) as held:
            assert held is not None
            reading = asyncio.create_task(
                client.post(
                    "/api/v1/chat/sessions" + endpoint,
                    json={"kb": "busy", "view_id": "legacy", "session_id": "busy-http"},
                    headers={"Authorization": "Bearer offline-read-token"},
                )
            )
            await asyncio.sleep(0.1)
            assert not reading.done()
            reading.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(reading, 1)


@pytest.mark.asyncio
async def test_cancelled_http_delete_does_not_run_after_the_active_session_unlocks(
    tmp_path, monkeypatch
):
    from ultrakb import api
    from ultrakb.application.conversations import read_conversation
    from ultrakb.locks import session_lock

    root, identity, _ = question_knowledge(tmp_path)
    store = SessionStore(root, identity)
    store.create("busy-delete", model="gpt-4o-mini", language="en")
    selected = storage_scope(root, identity)
    version = read_conversation(root, "busy-delete", scope=selected).version
    monkeypatch.setattr(api, "_resolve_kb", lambda name: root)
    monkeypatch.setattr(
        "ultrakb.api_views.view_scope",
        lambda *args, **kwargs: storage_scope(root, identity, cancelled=kwargs.get("cancelled")),
    )
    monkeypatch.setenv("ULTRAKB_API_TOKEN", "offline-delete-token")
    ready, release = Event(), Event()

    def hold():
        with session_lock(root, "busy-delete", database_identity=identity):
            ready.set()
            assert release.wait(5)

    holder = Thread(target=hold)
    holder.start()
    try:
        assert await asyncio.to_thread(ready.wait, 2)
        transport = httpx.ASGITransport(app=api.create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            deleting = asyncio.create_task(
                client.post(
                    "/api/v1/chat/sessions/delete",
                    json={
                        "kb": "busy",
                        "view_id": "legacy",
                        "session_id": "busy-delete",
                        "version": version,
                    },
                    headers={"Authorization": "Bearer offline-delete-token"},
                )
            )
            await asyncio.sleep(0.1)
            assert not deleting.done()
            deleting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(deleting, 1)
    finally:
        release.set()
        await asyncio.to_thread(holder.join, 2)
    assert read_conversation(root, "busy-delete", scope=selected).version == version
