"""The real spawn runtime preserves partial workbook receipts and retries only unfinished sheets."""

import json
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

pytest_plugins = ("test_workbook_import",)


def test_partial_workbook_task_can_retry_without_repeating_completed_sheets(kb_dir, three_sheets):
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.locks import atomic_write_text
    from openkb.runtime.requests import ImportFile
    from openkb.runtime.task_actions import preview_retry, retry_task
    from openkb.runtime.tasks import TaskManager

    failing = {"BETA_SHEET"}
    calls = Counter()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            prompt = str(body["messages"])
            markers = [
                name for name in ("ALPHA_SHEET", "BETA_SHEET", "GAMMA_SHEET") if name in prompt
            ]
            calls.update(markers)
            failure = any(name in failing for name in markers)
            self.send_response(400 if failure else 200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            value = (
                {"error": {"message": "Fixture refuses Beta", "type": "invalid_request_error"}}
                if failure
                else {
                    "id": "fixture",
                    "object": "chat.completion",
                    "created": 1,
                    "model": body["model"],
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {
                                        "description": "Worksheet fixture",
                                        "content": "Worksheet summary",
                                        "create": [],
                                        "update": [],
                                        "related": [],
                                    }
                                ),
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            )
            self.wfile.write(json.dumps(value).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    apply_kb_config_patch(
        kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"model": "openai/worksheet-fixture"})
    )
    atomic_write_text(
        kb_dir / ".env",
        f"LLM_API_KEY=fixture-key\nOPENAI_API_BASE=http://127.0.0.1:{server.server_port}/v1\n",
    )
    manager = TaskManager(history_dir=kb_dir / "history")
    try:
        task = manager.submit(kb_dir, [ImportFile(str(three_sheets))])
        first = manager.wait(task, timeout=30)
        assert first.state == "partial", first
        assert first.results[0].status == "partial"
        before = calls.copy()
        failing.clear()
        preview = preview_retry(manager, task)
        assert preview.allowed, preview.reason
        resumed = manager.wait(retry_task(manager, preview), timeout=30)
        assert resumed.state == "completed", resumed
        assert calls["ALPHA_SHEET"] == before["ALPHA_SHEET"]
        assert calls["GAMMA_SHEET"] == before["GAMMA_SHEET"]
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_workbook_waiting_for_version_keeps_task_blocked(kb_dir, three_sheets, pdf_model):
    import shutil

    from openkb.application.documents import import_document
    from openkb.runtime.requests import ImportFile
    from openkb.runtime.tasks import TaskManager
    from openkb.view_records import SourceMetadata

    metadata = SourceMetadata(product="Fixture", family="Worksheets")
    first = import_document(kb_dir, three_sheets, metadata=metadata)
    assert first.status == "added"
    copied = three_sheets.with_name("waiting.xlsx")
    shutil.copyfile(three_sheets, copied)
    manager = TaskManager(history_dir=kb_dir / "history")
    try:
        task = manager.submit(kb_dir, [ImportFile(str(copied), metadata=metadata)])
        outcome = manager.wait(task, timeout=30)
        assert outcome.state == "blocked", outcome
        assert outcome.results[0].status == "blocked"
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)
