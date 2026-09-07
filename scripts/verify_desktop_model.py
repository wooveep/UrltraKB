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
            + "Private URL preparation is compiled into a persistent knowledge base. " * 8
            + "</p></article></body></html>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
    parser.add_argument("--urls", action="store_true")
    args = parser.parse_args()
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
            else [sys.executable, "-m", "openkb.desktop.verification"]
        )
        command.extend(
            ["--output", args.output, "--model-base", f"http://127.0.0.1:{server.server_port}/v1"]
        )
        if args.inputs:
            command.extend(["--inputs", args.inputs])
        if args.urls:
            command.extend(["--url", f"http://127.0.0.1:{server.server_port}/article"])
            command.extend(["--one-shot-url", f"http://127.0.0.1:{server.server_port}/once.pdf"])
        subprocess.run(command, check=True, timeout=240)
        if args.urls:
            assert server.one_shot_gets == 1, "A complete download must survive KB lease deferral"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


if __name__ == "__main__":
    main()
