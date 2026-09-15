"""Retrieval and answer generation share one hard request allowance."""

import json

import pytest

from openkb.agent.query import build_run_config_from_bundle, run_query
from openkb.application.execution import ExecutionContext
from openkb.compilation_report import collect_compile_report
from openkb.config import load_config, save_config
from openkb.locks import atomic_write_text, kb_ingest_lock
from openkb.processing import ProcessingIncomplete


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_query_cannot_exceed_configured_request_allowance(kb_dir, model_service, stream):
    target = "sources/snapshots/v-p.md#block-port"
    atomic_write_text(kb_dir / "wiki/index.md", f"Port: 4100 [Original]({target})")

    def chat(body):
        if any(m["role"] == "tool" for m in body["messages"]):
            return {"role": "assistant", "content": f"Port: 4100 [Original]({target})"}
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "read",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "index.md"}),
                    },
                }
            ],
        }

    model_service.chat_response = chat
    with kb_ingest_lock(kb_dir / ".openkb"):
        config = load_config(kb_dir / ".openkb/config.yaml")
        config["processing"]["max_requests"] = 1
        save_config(kb_dir / ".openkb/config.yaml", config)
        with ExecutionContext().begin(kb_dir) as bundle, collect_compile_report() as report:
            with pytest.raises(ProcessingIncomplete):
                await run_query(
                    "Which port?",
                    kb_dir,
                    config["model"],
                    stream=stream,
                    bundle=bundle,
                    run_config=build_run_config_from_bundle(config["model"], bundle),
                )
    assert len(model_service) == 1
    assert report.usage["observable_attempts"] == 1
    assert report.usage["charged_tokens"] == 130
