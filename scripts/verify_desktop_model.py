"""Run native package checks against a controlled local HTTP model (no live credentials)."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def completion(body):
    """Replace only the model HTTP boundary, including PageIndex's model calls."""
    prompt = "\n".join(str(message.get("content", "")) for message in body["messages"])
    if "detect if there is a table of content" in prompt:
        return json.dumps({"toc_detected": "no"})
    if "expert in extracting hierarchical tree structure" in prompt:
        pages = sorted({int(page) for page in re.findall(r"<physical_index_(\d+)>", prompt)})
        return json.dumps(
            [
                {
                    "structure": str(page),
                    "title": f"PortableProbe 中文 PDF 第 {page} 页",
                    "physical_index": f"<physical_index_{page}>",
                }
                for page in pages
            ]
        )
    if "check if the given section appears or starts" in prompt:
        return json.dumps({"answer": "yes"})
    if "current section starts in the beginning" in prompt:
        return json.dumps({"start_begin": "yes"})
    if "generate a description of the partial document" in prompt:
        return "Native compiled PDF page summary"
    if "expert in generating descriptions for a document" in prompt:
        return "Native compiled long PDF with twenty sections"
    return json.dumps(
        {
            "description": "Native compiled fixture",
            "content": "# Native compiled\n\nDocument conversion and compilation fixture.",
            "concepts": {"create": [], "update": [], "related": []},
            "entities": {"create": [], "update": [], "related": []},
        }
    )


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
            content = completion(body)
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
