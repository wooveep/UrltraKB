"""Ephemeral browser previews and images from one retained catalog read."""

import io
import mimetypes
import posixpath
from dataclasses import dataclass, field
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from threading import Event, Lock, Thread
from types import MappingProxyType
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from ultrakb.knowledge_scope import KnowledgeScope
from ultrakb.locks import LockCancelled
from ultrakb.markdown_images import image_references
from ultrakb.storage.coordination import storage_stage

from .reading_assets import ReadingAssets, capture_reading_assets
from .storage_artifacts import StoredArtifact


@dataclass
class ArtifactPreview:
    url: str
    server: ThreadingHTTPServer
    thread: Thread
    stopped: Event
    _close_lock: Lock = field(default_factory=Lock, repr=False)
    _closed: bool = False

    def request_stop(self) -> None:
        self.stopped.set()

    def close(self) -> None:
        self.request_stop()
        with self._close_lock:
            if not self._closed:
                self.server.shutdown()
                self.server.server_close()
                self.thread.join()
                self._closed = True


class _LocalReferences(HTMLParser):
    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if value and name in {"href", "src", "srcset", "style", "data", "poster"}:
                if "file:" in value.lower():
                    raise ValueError(
                        "Browser previews require retained relative artifact references"
                    )


def _preview_content(root, path, scope, epoch, stopped, *, cancelled=lambda: False):
    from ultrakb.storage.artifact_store import ArtifactStore
    from ultrakb.storage.blobs import copy_blob

    assert scope.knowledge is not None

    def stopping():
        return stopped.is_set() or cancelled()

    with storage_stage(root, scope.knowledge.identity, read_only=True, cancelled=stopping) as db:
        if path.startswith("wiki/"):
            digest = scope.knowledge.file_digest(path.removeprefix("wiki/"))
        else:
            digest = ArtifactStore(root, scope.knowledge.identity).entry(path, epoch=epoch).digest
        output = io.BytesIO()
        copy_blob(db, digest, output, cancelled=stopping)
        content = output.getvalue()
        if Path(path).suffix.lower() in {".html", ".htm"}:
            _LocalReferences().feed(content.decode("utf-8"))
        return content


def prepare_artifact_preview(
    root: Path,
    relative: str,
    file: str,
    *,
    scope: KnowledgeScope,
    epoch: int | None = None,
    cancelled=lambda: False,
) -> ArtifactPreview:
    """Serve retained dependencies on loopback; no URL can read the working copy."""
    if Path(file).suffix.lower() not in {".html", ".htm"}:
        raise ValueError("Select an HTML file to preview")
    if scope.knowledge is None:
        raise ValueError("Retained previews require a main-store scope")
    stopped = Event()
    with storage_stage(root, scope.knowledge.identity, read_only=True, cancelled=cancelled):
        selected = StoredArtifact(root, relative, scope, epoch=epoch)
        if file not in selected.paths():
            raise ValueError("Select a file in the retained artifact")
        epoch = selected.epoch
        _preview_content(root, file, scope, epoch, stopped, cancelled=cancelled)
    if cancelled():
        raise InterruptedError("Artifact preview cancelled")
    prefix = "/" + uuid4().hex + "/"

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(0.5)
            super().setup()

        def do_GET(self):
            from ultrakb.storage.wiki_paths import wiki_path

            try:
                path = unquote(urlsplit(self.path).path)
                if not path.startswith(prefix) or stopped.is_set():
                    raise FileNotFoundError(path)
                path = path.removeprefix(prefix)
                wiki_path(path)
                payload = _preview_content(root, path, scope, epoch, stopped)
            except (FileNotFoundError, ValueError):
                self.send_error(404, "Retained preview resource unavailable")
                return
            except (InterruptedError, LockCancelled):
                self.send_error(503, "Preview closed")
                return
            self.send_response(200)
            self.send_header(
                "Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream"
            )
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self' https: http: data: blob: 'unsafe-inline' 'unsafe-eval'; "
                "base-uri 'self'; object-src 'none'",
            )
            self.end_headers()
            if not stopped.is_set():
                self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = False
    thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    from urllib.parse import quote

    return ArtifactPreview(
        f"http://127.0.0.1:{server.server_port}{prefix}{quote(file, safe='/')}",
        server,
        thread,
        stopped,
    )


def capture_artifact_assets(
    root: Path, relative: str, content: str, *, scope: KnowledgeScope, epoch: int
) -> ReadingAssets:
    """Capture Markdown images; an unknown reference cannot use the working copy."""
    from ultrakb.storage.artifact_store import ArtifactStore

    if scope.knowledge is None:
        raise ValueError("Retained artifact images require a main-store scope")
    if relative.startswith("wiki/"):
        return capture_reading_assets(scope, relative.removeprefix("wiki/"), content)
    images = {}
    store = ArtifactStore(root, scope.knowledge.identity)
    for reference in image_references(content):
        if reference.local_path is None:
            continue
        logical = posixpath.normpath(str(PurePosixPath(relative).parent / reference.local_path))
        if logical in images or not logical.startswith("output/"):
            continue
        try:
            images[logical] = store.read(logical, epoch=epoch)
        except (FileNotFoundError, ValueError):
            continue
    return ReadingAssets(
        root / PurePosixPath(relative).parent,
        f"artifacts-{scope.knowledge.publication_id}-{epoch}",
        relative,
        MappingProxyType(images),
    )
