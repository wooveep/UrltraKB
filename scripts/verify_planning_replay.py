"""Replay a credential-free captured request without a KB, network or model sends."""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()

    def forbidden(*args, **kwargs):
        raise AssertionError("Captured planning replay cannot access the network")

    socket.socket.connect = forbidden
    socket.socket.connect_ex = forbidden
    socket.create_connection = forbidden
    from ultrakb.agent.compile_capacity import CompileCapacity
    from ultrakb.agent.planning_capacity import fit_planning_messages
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.execution_capacity import require_request_capacity
    from ultrakb.llm_execution import CompletionExecutor, RoleBindings
    from ultrakb.locks import atomic_write_json
    from ultrakb.model_tokenizer import TokenMeasure

    content = options.capture.read_bytes()
    frozen = json.loads(content)
    payload, policy = frozen["payload"], frozen["policy"]
    measure = TokenMeasure(policy)
    executor = CompletionExecutor(
        RoleBindings(
            policy["model"],
            LlmCredentialBundle(),
            generation={"compile": {"max_tokens": payload["max_tokens"]}},
        )
    )
    sizing = CompileCapacity(executor, measure)
    messages = payload["messages"]
    header, navigation = messages[1]["content"].split("\n", 1)
    nodes = json.loads(navigation)
    expected = [
        {key: value for key, value in node.items() if key != "navigation_summary"} for node in nodes
    ]
    compact = {"role": "user", "content": header + "\n" + json.dumps(expected, ensure_ascii=False)}
    assert sizing.payload(messages) == payload, "Captured full wire input changed"
    admitted = fit_planning_messages(messages, sizing, compact_document_message=compact)
    actual = json.loads(admitted[1]["content"].split("\n", 1)[1])
    assert actual == expected, "Native IDs, titles, hierarchy or ranges changed"
    result = sizing.payload(admitted)
    require_request_capacity(measure, result, operation="compile")
    assert executor.calls == 0 and options.capture.read_bytes() == content
    atomic_write_json(
        options.output,
        {
            "capture_sha256": hashlib.sha256(content).hexdigest(),
            "capture_unchanged": True,
            "native_nodes": len(nodes),
            "exact_native_identity_preserved": True,
            "original_input_units": measure.payload_tokens(payload),
            "admitted_input_units": measure.payload_tokens(result),
            "input_limit": measure.input_limit,
            "measurement_unit": policy["measurement_unit"],
            "provider_sends": 0,
            "summary_checkpoint_reuse": (
                "not rerun; successful import already cleaned transient checkpoints"
            ),
        },
    )


if __name__ == "__main__":
    main()
