"""Browser preview dependencies remain in one retained main-store read scope."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import urlopen

import pytest
from test_storage_artifact_exports import artifact_writer

from ultrakb.application.artifact_previews import prepare_artifact_preview
from ultrakb.application.views import storage_scope
from ultrakb.storage import coordination
from ultrakb.storage.knowledge_store import KnowledgeStore
from ultrakb.storage.maintenance import Maintenance

pytestmark = pytest.mark.integration


def test_initial_html_copy_stops_between_chunks_before_the_preview_is_owned(tmp_path, monkeypatch):
    import ultrakb.storage.blobs as blobs

    root, identity, _, save = artifact_writer(tmp_path)
    save("output/large.html", b"<p>" + b"a" * (2 * 1024 * 1024) + b"</p>")
    stopped, copied = Event(), []
    original = blobs.copy_blob

    class StopAfterChunk:
        def __init__(self, output):
            self.output = output

        def write(self, payload):
            copied.append(len(payload))
            stopped.set()
            return self.output.write(payload)

    def copy(db, digest, output, **kwargs):
        return original(db, digest, StopAfterChunk(output), **kwargs)

    monkeypatch.setattr(blobs, "copy_blob", copy)
    with pytest.raises(InterruptedError):
        prepare_artifact_preview(
            root,
            "output/large.html",
            "output/large.html",
            scope=storage_scope(root, identity),
            cancelled=stopped.is_set,
        )
    assert copied == [1024 * 1024]


def test_single_file_preview_reads_retained_dependencies_and_joins_a_blocked_reader(
    tmp_path, monkeypatch
):
    root, identity, store, save = artifact_writer(tmp_path)
    html = b'<link rel="stylesheet" href="theme.css"><p>Retained page</p>'
    save("output/value.html", html)
    save("output/theme.css", b"p { color: blue }")
    knowledge = KnowledgeStore(root, identity)
    knowledge.publish(
        knowledge.prepare_changes(knowledge.bind(), {"reports/note.md": b"# Original report"})
    )
    scope = storage_scope(root, identity)
    preview = prepare_artifact_preview(
        root, "output/value.html", "output/value.html", scope=scope, epoch=store.epoch()
    )
    save("output/theme.css", b"p { color: red }")
    knowledge.publish(
        knowledge.prepare_changes(knowledge.bind(), {"reports/note.md": b"# Later report"})
    )
    try:
        with urlopen(preview.url, timeout=2) as response:
            assert response.read() == html
        with urlopen(urljoin(preview.url, "theme.css"), timeout=2) as response:
            assert response.read() == b"p { color: blue }"
        with urlopen(urljoin(preview.url, "../wiki/reports/note.md"), timeout=2) as response:
            assert response.read() == b"# Original report"
        with pytest.raises(HTTPError) as rejected:
            urlopen(urljoin(preview.url, "../.ultrakb/kb.sqlite"), timeout=2)
        assert rejected.value.code == 404
        assert Maintenance(root, identity).collect(max_items=1).status == "completed"
        entered = Event()
        original = coordination.storage_stage

        @contextmanager
        def observed(*args, **kwargs):
            entered.set()
            with original(*args, **kwargs) as db:
                yield db

        monkeypatch.setattr("ultrakb.application.artifact_previews.storage_stage", observed)
        with coordination.try_reclamation(root, identity) as db:
            assert db is not None
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(urlopen, preview.url, timeout=3)
                assert entered.wait(2) and not pending.done()
                preview.close()
                with pytest.raises(HTTPError) as closed:
                    pending.result(timeout=2)
                assert closed.value.code == 503
                assert not preview.thread.is_alive()
    finally:
        preview.close()


@pytest.mark.parametrize("reference", ["href", "src", "style"])
def test_preview_rejects_absolute_file_dependencies_before_opening_a_browser(tmp_path, reference):
    root, identity, _, save = artifact_writer(tmp_path)
    path = (root / "output/theme.css").as_uri()
    value = f"url('{path}')" if reference == "style" else path
    save("output/value.html", f'<base {reference}="{value}"><p>Retained page</p>'.encode())
    with pytest.raises(ValueError, match="retained relative artifact"):
        prepare_artifact_preview(
            root, "output/value.html", "output/value.html", scope=storage_scope(root, identity)
        )
