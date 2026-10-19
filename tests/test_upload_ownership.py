"""Slow uploads are private; a consumer may only discard its own unregistered raw."""

import asyncio
import io

import pytest
from starlette.datastructures import UploadFile

from ultrakb.api_helpers import _add_saved_file, _reserve_add_uploads, _write_add_uploads
from ultrakb.application.documents import AddFileResult
from ultrakb.application.views import published_scope
from ultrakb.storage.catalog_store import CatalogStore


def test_upload_body_is_invisible_to_raw_watch_until_complete(kb_dir, monkeypatch):
    class SlowUpload(UploadFile):
        async def read(self, size=-1):
            assert not list((kb_dir / "raw").iterdir())
            return await super().read(size)

    async def run():
        upload = SlowUpload(filename="中文.md", file=io.BytesIO(b"# complete input"))
        reserved = _reserve_add_uploads(kb_dir, [upload])
        ready = await _write_add_uploads(reserved, [upload])
        assert not list((kb_dir / "raw").iterdir())
        result = await _add_saved_file(kb_dir, *ready[0])
        assert result.status == "added"

    def consume(path, root, **kwargs):
        assert path.parent == root / "raw" and path.read_bytes() == b"# complete input"
        return AddFileResult(path.name, str(path), "added", "Added")

    monkeypatch.setattr("ultrakb.api_helpers._add_for_api", consume)
    asyncio.run(run())


def test_skipped_cleanup_keeps_a_raw_file_registered_during_the_operation(kb_dir, monkeypatch):
    async def run():
        upload = UploadFile(filename="paper.md", file=io.BytesIO(b"# original"))
        ready = await _write_add_uploads(_reserve_add_uploads(kb_dir, [upload]), [upload])
        assert (await _add_saved_file(kb_dir, *ready[0])).status == "skipped"

    def consume(path, root, **kwargs):
        CatalogStore(root, published_scope(root).knowledge.identity).admit(
            path, source_key=str(path.resolve())
        )
        return AddFileResult(path.name, None, "skipped", "Already registered")

    monkeypatch.setattr("ultrakb.api_helpers._add_for_api", consume)
    asyncio.run(run())
    assert (kb_dir / "raw/paper.md").read_bytes() == b"# original"


def test_cancelled_body_cleans_only_owned_private_files(kb_dir):
    class CancelledUpload(UploadFile):
        async def read(self, size=-1):
            raise asyncio.CancelledError()

    existing = kb_dir / "raw/paper.md"
    existing.write_text("keep")

    async def run():
        upload = CancelledUpload(filename="paper.md", file=io.BytesIO(b"unused"))
        ready = _reserve_add_uploads(kb_dir, [upload])
        with pytest.raises(asyncio.CancelledError):
            await _write_add_uploads(ready, [upload])
        assert not ready[0][0].exists()

    asyncio.run(run())
    assert existing.read_text() == "keep"


def test_disconnect_after_first_stream_event_removes_private_inputs(kb_dir):
    from ultrakb.api_helpers import _stream_add_uploads

    async def run():
        upload = UploadFile(filename="paper.md", file=io.BytesIO(b"# original"))
        ready = await _write_add_uploads(_reserve_add_uploads(kb_dir, [upload]), [upload])
        stream = _stream_add_uploads("kb", kb_dir, ready)
        assert "event: start" in await anext(stream)
        await stream.aclose()
        assert not ready[0][0].exists()
        assert not list((kb_dir / "raw").iterdir())

    asyncio.run(run())


def test_disconnect_retracts_upload_waiting_for_kb_lease(kb_dir, monkeypatch):
    import threading

    from ultrakb.api_helpers import _stream_add_uploads
    from ultrakb.locks import kb_ingest_lock

    acquired, release = threading.Event(), threading.Event()
    calls = []

    def hold():
        with kb_ingest_lock(kb_dir / ".ultrakb"):
            acquired.set()
            assert release.wait(10)

    async def run():
        upload = UploadFile(filename="paper.md", file=io.BytesIO(b"# original"))
        ready = await _write_add_uploads(_reserve_add_uploads(kb_dir, [upload]), [upload])
        stream = _stream_add_uploads("kb", kb_dir, ready)
        await anext(stream)
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 2)
        assert not ready[0][0].exists()

    monkeypatch.setattr(
        "ultrakb.api_helpers._add_for_api", lambda *args, **kwargs: calls.append(args)
    )
    holder = threading.Thread(target=hold)
    holder.start()
    assert acquired.wait(5)
    try:
        asyncio.run(run())
    finally:
        release.set()
        holder.join(5)
    assert not calls and not list((kb_dir / "raw").iterdir())


def test_upload_does_not_follow_dangling_raw_symlink(kb_dir, tmp_path):
    import os

    from ultrakb.application.uploads import published_input

    if os.name == "nt":
        pytest.skip("POSIX symlink fixture")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    (kb_dir / "raw/paper.md").symlink_to(outside)
    source = tmp_path / "paper.md"
    source.write_text("uploaded")
    with published_input(kb_dir, source) as result:
        assert result.path.name == "paper-1.md"
    assert not outside.exists()


def test_disconnect_before_iterator_starts_removes_private_upload(kb_dir):
    from ultrakb.api_helpers import _stream_add_uploads
    from ultrakb.api_uploads import UploadStreamingResponse

    async def run():
        upload = UploadFile(filename="paper.md", file=io.BytesIO(b"# original"))
        ready = await _write_add_uploads(_reserve_add_uploads(kb_dir, [upload]), [upload])
        response = UploadStreamingResponse(_stream_add_uploads("kb", kb_dir, ready), uploads=ready)

        async def send(message):
            raise OSError("Disconnected")

        async def receive():
            return {"type": "http.disconnect"}

        with pytest.raises(Exception):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert not ready[0][0].exists() and not ready[0][0].parent.exists()

    asyncio.run(run())
