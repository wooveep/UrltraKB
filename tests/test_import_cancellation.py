"""Safe stop must leave a slow model request without committing the import."""

import json
import multiprocessing
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import fitz
import pytest
import yaml
from processing_fixtures import configure_processing

from openkb.runtime.requests import ImportFile
from openkb.runtime.tasks import TaskManager


@pytest.fixture
def slow_model():
    arrived, release = threading.Event(), threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body["model"])
            arrived.set()
            release.wait(20)
            payload = json.dumps(
                {
                    "id": "test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": body["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "{}"},
                            "finish_reason": "stop",
                        }
                    ],
                }
            ).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except ConnectionError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", arrived, release, requests
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.mark.parametrize("suffix", ["pdf", "docx"])
def test_stop_import_during_model_wait_rolls_back_and_reaps_worker(
    kb_dir, tmp_path, slow_model, suffix
):
    url, arrived, release, calls = slow_model
    source = tmp_path / f"long.{suffix}"
    if suffix == "pdf":
        with fitz.open() as pdf:
            for number in range(2):
                pdf.new_page().insert_text((72, 72), f"Chapter {number + 1}. Document content.")
            pdf.save(source)
    else:
        from zipfile import ZipFile

        # Minimal real DOCX accepted by MarkItDown; no extra test dependency.
        mime = "application/vnd.openxmlformats-officedocument."
        rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        with ZipFile(source, "w") as archive:
            archive.writestr(
                "[Content_Types].xml",
                f"""
                <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
                  <Default Extension="rels"
                    ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
                  <Override PartName="/word/document.xml"
                    ContentType="{mime}wordprocessingml.document.main+xml"/>
                </Types>""",
            )
            archive.writestr(
                "_rels/.rels",
                f"""
                <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                  <Relationship Id="rId1" Type="{rel_ns}/officeDocument"
                    Target="word/document.xml"/>
                </Relationships>""",
            )
            archive.writestr(
                "word/document.xml",
                """
                <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
                  <w:body><w:p><w:r><w:t>Document content.</w:t></w:r></w:p></w:body>
                </w:document>""",
            )
    (kb_dir / ".openkb/config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": "openai/stop-test",
                "language": "en",
                "pageindex_threshold": 1,
                "timeout": 30,
            }
        )
    )
    (kb_dir / ".env").write_text(f"LLM_API_KEY=synthetic-test\nOPENAI_API_BASE={url}\n")
    configure_processing(kb_dir)
    (kb_dir / "wiki/concepts/previous.md").write_text("Previously committed document")
    wiki_before = {
        p.relative_to(kb_dir): p.read_bytes() for p in (kb_dir / "wiki").rglob("*") if p.is_file()
    }
    manager = TaskManager(history_dir=tmp_path / "history")
    task = manager.submit(kb_dir, [ImportFile(str(source)), ImportFile(str(source))])
    try:
        assert arrived.wait(15), manager.get(task)
        manager.stop(task)
        try:
            result = manager.wait(task, timeout=3)
        except TimeoutError:
            pytest.fail("Safe stop is stuck waiting for the model response")
        assert result.state == "stopped" and result.processes_reaped
        document = result.results[0].document
        assert document is not None and document.source_intake == "saved"
        assert document.knowledge_compilation == "stopped"
        from openkb.application.source_history import source_status

        saved = source_status(kb_dir, document.source_id)
        assert saved["result"]["knowledge_compilation"] == "stopped"
        assert saved["cumulative_usage"]["observable_attempts"] == 1
        assert result.succeeded == 0 and result.failed == 0 and result.unfinished == 2
        assert calls == ["stop-test"], "Cancellation started retries or another document"
        assert {
            p.relative_to(kb_dir): p.read_bytes()
            for p in (kb_dir / "wiki").rglob("*")
            if p.is_file()
        } == wiki_before
        assert not (kb_dir / "raw" / source.name).exists()
        assert not list((kb_dir / ".openkb/journal").glob("*.json"))
    finally:
        # Only this fixture's workers: make a red test terminate deterministically.
        for process in multiprocessing.active_children():
            if process.name.startswith(f"openkb-unit-{task[:8]}-"):
                process.terminate()
        release.set()
        manager.shutdown(stop=True)
        assert manager.join(10)


def test_async_model_cancellation_is_not_retried(slow_model):
    import asyncio

    import litellm

    from openkb.cancellation import OperationCancelled
    from openkb.runtime.model_cancellation import DocumentCancellation

    url, arrived, release, calls = slow_model
    stopped = threading.Event()

    def stop_on_request():
        if arrived.wait(10):
            stopped.set()

    observer = threading.Thread(target=stop_on_request, daemon=True)
    observer.start()
    original = litellm.acompletion

    async def run():
        # PageIndex gathers per-node exceptions. The commit checkpoint must
        # still observe cancellation even if the gather absorbs this exception.
        return await asyncio.gather(
            litellm.acompletion(
                model="openai/stop-test",
                api_key="synthetic-test",
                api_base=url,
                messages=[{"role": "user", "content": "fixture"}],
                timeout=30,
            ),
            return_exceptions=True,
        )

    try:
        with DocumentCancellation(stopped) as cancellation:
            cancellation.install(litellm)
            results = asyncio.run(asyncio.wait_for(run(), timeout=5))
        assert isinstance(results[0], OperationCancelled)
        assert litellm.acompletion is original
        assert calls == ["stop-test"]
    finally:
        release.set()
        observer.join(10)


@pytest.mark.parametrize("coordinator", ["add", "mutation"])
def test_stop_at_commit_rolls_back_even_if_callee_absorbs_cancellation(kb_dir, coordinator):
    from openkb.add_coordinator import AddMutationPlan, run_add_mutation
    from openkb.cancellation import OperationCancelled, cancellation_scope
    from openkb.locks import atomic_write_text, kb_ingest_lock
    from openkb.mutation import mutation_scope

    page = kb_dir / "wiki/concepts/previous.md"
    page.write_text("Previously committed")
    stopped = threading.Event()

    def body(_):
        atomic_write_text(page, "Uncommitted change")
        stopped.set()

    with kb_ingest_lock(kb_dir / ".openkb"), cancellation_scope(stopped.is_set):
        with pytest.raises(OperationCancelled):
            if coordinator == "add":
                run_add_mutation(kb_dir, AddMutationPlan("add", {}, [page], body))
            else:
                with mutation_scope(kb_dir, [page], operation="recompile") as snapshot:
                    body(snapshot)
    assert page.read_text() == "Previously committed"
    assert not list((kb_dir / ".openkb/journal").glob("*.json"))


def test_failed_cancellation_rollback_requires_repair(kb_dir, monkeypatch):
    from openkb.add_coordinator import AddMutationPlan, DirtyRollbackError, run_add_mutation
    from openkb.cancellation import OperationCancelled
    from openkb.locks import kb_ingest_lock
    from openkb.mutation import MutationSnapshot, repair_marker

    def cancel(_):
        raise OperationCancelled()

    monkeypatch.setattr(MutationSnapshot, "rollback_best_effort", lambda _: OSError("fixture"))
    with kb_ingest_lock(kb_dir / ".openkb"), pytest.raises(DirtyRollbackError):
        run_add_mutation(kb_dir, AddMutationPlan("add", {}, [], cancel))
    assert repair_marker(kb_dir).exists()
    assert list((kb_dir / ".openkb/journal").glob("*.json"))
