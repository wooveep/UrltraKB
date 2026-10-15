"""Run native package checks with genuine DeepSeek Flash and a hard 2 CNY budget."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/once.pdf":
            self.server.one_shot_gets += 1
            if self.server.one_shot_gets > 1:
                self.send_error(410)
                return
            body = self.server.pdf_bytes
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path != "/article":
            self.send_error(404)
            return
        body = (
            "<!doctype html><html><head><title>Native URL acquisition</title></head>"
            "<body><article><h1>Native URL acquisition</h1><p>"
            + "Private URL preparation is compiled into a persistent knowledge base."
            + "</p></article></body></html>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--program", help="Frozen UltraKBVerify executable; otherwise use source")
    parser.add_argument("--output", required=True)
    parser.add_argument("--inputs")
    parser.add_argument("--urls", action="store_true")
    parser.add_argument(
        "--llm-cost-report",
        type=Path,
        help="Shared test-run ledger; every request is admitted under a hard 2 CNY limit",
    )
    args = parser.parse_args()
    # Native acceptance uses the same gateway, raw responses and billing guard
    # as pytest. Only the document acquisition endpoints below are local fixtures.
    sys.path.insert(0, str(ROOT / "tests"))
    from live_llm import LiveLLM
    from live_network import install_network_guard
    from llm_test_budget import TestRunBudget

    install_network_guard()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report = args.llm_cost_report or (
        ROOT / ".scratch/llm-test-runs" / f"desktop-{stamp}-{uuid4().hex[:8]}" / "cost.json"
    )
    budget = TestRunBudget(report)
    model = LiveLLM(budget)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    if args.urls:
        import pymupdf

        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((72, 72), "One-time URL input survives waiting for a knowledge base.")
            server.pdf_bytes = document.tobytes()
        server.one_shot_gets = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        command = (
            [args.program]
            if args.program
            else [sys.executable, "-m", "ultrakb.desktop.verification"]
        )
        command.extend(["--output", args.output, "--model-base", model.credentials.base_url])
        if args.inputs:
            command.extend(["--inputs", args.inputs])
        if args.urls:
            command.extend(["--url", f"http://127.0.0.1:{server.server_port}/article"])
            command.extend(["--one-shot-url", f"http://127.0.0.1:{server.server_port}/once.pdf"])
        environment = {**os.environ, "ULTRAKB_VERIFY_LLM_API_KEY": model.credentials.api_key}
        subprocess.run(command, check=True, timeout=600, env=environment)
        if args.urls:
            assert server.one_shot_gets == 1, "A complete download must survive KB lease deferral"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        model.close()
        print(json.dumps({"llm_cost_report": str(report.resolve()), **budget.report()}))


if __name__ == "__main__":
    main()
