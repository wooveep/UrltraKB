"""Local HTTP fixture exercises actual SDK serialization; it is not a model-service test."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def run_transport():
    requests = []

    class Fixture(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"path": self.path, "stream": bool(body.get("stream"))})
            self.send_response(200)
            if body.get("stream"):
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for text, reason in [("PortableProbe ", None), ("stream", None), ("", "stop")]:
                    chunk = {
                        "id": "probe",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": body["model"],
                        "choices": [
                            {"index": 0, "delta": {"content": text}, "finish_reason": reason}
                        ],
                    }
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {
                            "id": "probe",
                            "object": "chat.completion",
                            "created": 1,
                            "model": body["model"],
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {
                                        "role": "assistant",
                                        "content": "PortableProbe SDK",
                                    },
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 1,
                                "completion_tokens": 2,
                                "total_tokens": 3,
                            },
                        }
                    ).encode()
                )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}/v1"
    try:
        import litellm

        stream = litellm.completion(
            model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": "probe"}],
            api_base=base,
            api_key="prototype-not-a-credential",
            stream=True,
        )
        text = "".join(c.choices[0].delta.content or "" for c in stream)
        assert text == "PortableProbe stream", text
        from agents import Agent, OpenAIChatCompletionsModel, Runner, set_tracing_disabled
        from openai import AsyncOpenAI

        set_tracing_disabled(True)

        async def agents_call():
            async with AsyncOpenAI(base_url=base, api_key="prototype-not-a-credential") as client:
                model = OpenAIChatCompletionsModel(model="gpt-4o-mini", openai_client=client)
                response = await Runner.run(
                    Agent(name="Packaging probe", model=model), input="probe"
                )
                return response.final_output

        final = asyncio.run(agents_call())
        assert final == "PortableProbe SDK", final
        return {
            "fixture_only": True,
            "live_model_evaluated": False,
            "litellm_stream": text,
            "agents_response": final,
            "requests": requests,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
