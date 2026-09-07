"""Run native package checks against a controlled local HTTP model (no live credentials)."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(200)
        if body.get("stream"):
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for content, reason in [("Native ", None), ("answer", None), ("", "stop")]:
                event = {
                    "id": "native-verification",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": body["model"],
                    "choices": [
                        {"index": 0, "delta": {"content": content}, "finish_reason": reason}
                    ],
                }
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            content = json.dumps(
                {
                    "description": "Native compiled fixture",
                    "content": "# Native compiled\n\nDocument conversion and compilation fixture.",
                    "concepts": {"create": [], "update": [], "related": []},
                    "entities": {"create": [], "update": [], "related": []},
                }
            )
            event = {
                "id": "native-verification",
                "object": "chat.completion",
                "created": 1,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            }
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(event).encode())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--program", help="Frozen OpenKBVerify executable; otherwise use source")
    parser.add_argument("--output", required=True)
    parser.add_argument("--inputs")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        command = (
            [args.program]
            if args.program
            else [sys.executable, "-m", "openkb.desktop.verification"]
        )
        command.extend(
            ["--output", args.output, "--model-base", f"http://127.0.0.1:{server.server_port}/v1"]
        )
        if args.inputs:
            command.extend(["--inputs", args.inputs])
        subprocess.run(command, check=True, timeout=240)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


if __name__ == "__main__":
    main()
