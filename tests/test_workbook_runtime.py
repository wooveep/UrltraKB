"""The real spawn runtime preserves partial workbook receipts and retries only unfinished sheets."""

import json
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from test_workbook_import import worksheet_prompt_markers

pytest_plugins = ("test_office_import", "block_fixtures", "workbook_fixtures")


def test_partial_workbook_task_can_retry_without_repeating_completed_sheets(
    kb_dir, three_sheets, live_llm, monkeypatch
):
    from office_publication_fixtures import publish_workbook

    from ultrakb.application.settings import apply_kb_config_patch
    from ultrakb.application.settings_data import KbConfigPatchRequest
    from ultrakb.application.sources import source_inventory
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.locks import atomic_write_text
    from ultrakb.runtime.requests import ImportFile
    from ultrakb.runtime.task_actions import preview_retry, retry_task
    from ultrakb.runtime.tasks import TaskManager

    live_llm.install(monkeypatch)
    credentials = live_llm.credentials
    failing = {"BETA_SHEET"}
    calls = Counter()
    http_requests = 0

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            nonlocal http_requests
            http_requests += 1
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            markers = worksheet_prompt_markers(body["messages"])
            calls.update(markers)
            failure = any(name in failing for name in markers)
            if failure:
                # A deliberate local transport rejection exercises the worker's
                # nonretryable path; successful model data is always forwarded.
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {"error": {"message": "Local worksheet transport rejection"}}
                    ).encode()
                )
                return
            with httpx.Client(timeout=90) as client:
                upstream = client.post(
                    credentials.base_url + "/chat/completions",
                    json=body,
                    headers={"Authorization": self.headers["Authorization"]},
                )
            self.send_response(upstream.status_code)
            self.send_header("Content-Type", upstream.headers["Content-Type"])
            self.end_headers()
            self.wfile.write(upstream.content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    apply_kb_config_patch(
        kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"model": "deepseek/deepseek-flash"})
    )
    atomic_write_text(
        kb_dir / ".env",
        f"LLM_API_KEY={credentials.api_key}\n"
        f"OPENAI_API_BASE=http://127.0.0.1:{server.server_port}/v1\n",
    )
    prepared = publish_workbook(
        kb_dir,
        three_sheets,
        bundle=LlmCredentialBundle(
            api_key=credentials.api_key,
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
        ),
        sheet_names=("Alpha",),
        record_model_task=False,
    )
    assert prepared.status == "added" and len(prepared.units) == 1
    assert prepared.units[0].name == "Alpha" and prepared.units[0].status == "completed"
    assert not list((kb_dir / ".ultrakb/model-tasks").glob("*.json"))
    assert http_requests == 0
    manager = TaskManager(history_dir=kb_dir / "history")
    try:
        task = manager.submit(kb_dir, [ImportFile(str(three_sheets))])
        first = manager.wait(task, timeout=90)
        assert first.state == "failed", first
        assert first.results[0].status == "failed"
        assert calls["GAMMA_SHEET"] == 0  # Nonretryable local rejection stops later sheets.
        before = calls.copy()
        alpha = next(
            unit for unit in source_inventory(kb_dir)[0]["units"] if unit["name"] == "Alpha"
        )
        assert alpha["status"] == "completed"
        assert alpha["knowledge_revision_id"] == prepared.units[0].knowledge_revision_id
        failing.clear()
        preview = preview_retry(manager, task)
        assert preview.allowed, preview.reason
        resumed = manager.wait(retry_task(manager, preview), timeout=90)
        assert resumed.state == "completed", resumed
        retained = next(unit for unit in resumed.results[0].import_units if unit.name == "Alpha")
        assert retained.knowledge_revision_id == alpha["knowledge_revision_id"]
        assert retained.model_usage["current"]["requests"] == 0
        assert calls["GAMMA_SHEET"] > before["GAMMA_SHEET"]
        journals = list((kb_dir / ".ultrakb/model-tasks").glob("*.json"))
        assert len(journals) == 1
        assert json.loads(journals[0].read_text())["used"] == http_requests
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)
        server.shutdown()
        server.server_close()
        thread.join(5)
