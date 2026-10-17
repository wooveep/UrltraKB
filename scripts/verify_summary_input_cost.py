"""Compare summary input shapes offline; local units are never provider billing."""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()

    def forbidden(*args, **kwargs):
        raise AssertionError("Summary input comparison cannot access the network")

    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = forbidden
    from ultrakb.agent.compile_capacity import CompileCapacity
    from ultrakb.agent.document_summary import SYSTEM_TEMPLATE, summary_messages
    from ultrakb.agent.source_excerpts import SourceExcerpts
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.execution_capacity import capacity_policy
    from ultrakb.llm_execution import CompletionExecutor, RoleBindings
    from ultrakb.locks import atomic_write_json, atomic_write_text
    from ultrakb.model_tokenizer import TokenMeasure

    original = "\n\n".join(
        f"## Step {number}\nClose valve {number} before maintenance. Reopen only after pressure "
        "reaches zero. Keep the vent closed except when an authorized purge requires it open."
        for number in range(1, 25)
    )
    model = "deepseek/deepseek-flash"
    policy = capacity_policy(
        {
            "model": model,
            "model_capacity": {"max_input_tokens": 65536, "tokenizer_model": "gpt-4o-mini"},
        },
        custom_endpoint=False,
    )
    measure = TokenMeasure(policy)
    executor = CompletionExecutor(RoleBindings(model, LlmCredentialBundle()))
    sizing = CompileCapacity(executor, measure)
    request_options = {"max_tokens": 512, "response_format": {"type": "json_object"}}
    system = {"role": "system", "content": SYSTEM_TEMPLATE.format(language="en")}

    def cost(messages):
        payload = sizing.payload(messages, request_options)
        return {
            "local_input_units": measure.payload_tokens(payload),
            "serialized_bytes": len(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
        }

    with tempfile.TemporaryDirectory(prefix="ultrakb-summary-input-") as name:
        wiki = Path(name)
        atomic_write_text(wiki / "sources/manual.md", original)
        atomic_write_text(wiki / "summaries/manual.md", "---\nfull_text: sources/manual.md\n---\n")
        reader = CompileSourceWindows(wiki, "manual", navigation=[])
        variants = []
        for maximum in (800, 1600):
            windows = list(
                reader.iter_document_windows(fits=lambda w: len(w["content"]) <= maximum)
            )
            assert "".join(window["content"] for window in windows) == original
            for structured in (False, True):
                requests = [
                    summary_messages(
                        system, "manual", window, structured=structured, request_format=3
                    )
                    for window in windows
                ]
                sizes = [cost(messages) for messages in requests]
                variants.append(
                    {
                        "maximum_primary_characters": maximum,
                        "reference_expression": "structured_refs" if structured else "markers",
                        "requests": len(requests),
                        "complete_primary_source_preserved": True,
                        "total_local_input_units": sum(s["local_input_units"] for s in sizes),
                        "total_serialized_bytes": sum(s["serialized_bytes"] for s in sizes),
                        "per_request": sizes,
                    }
                )
                if maximum == 800 and structured:
                    first, window = requests[0], windows[0]
                    excerpts = SourceExcerpts([window, *window["supplementary_context"]])
                    assert excerpts.messages(first, structured=True) == first
                    deduplication = {
                        "actual_idempotent_catalogue": cost(first),
                        "counterfactual_duplicate_catalogue": cost(
                            [*first, excerpts.reference_message()]
                        ),
                        "primary_and_neighboring_originals_unchanged": True,
                    }
    assert executor.calls == 0
    atomic_write_json(
        options.output,
        {
            "source_sha256": hashlib.sha256(original.encode()).hexdigest(),
            "source_characters": len(original),
            "measurement_policy": policy,
            "measurement": "Local complete-wire input units; not provider-billed tokens.",
            "provider_sends": 0,
            "window_and_reference_variants": variants,
            "deduplication_assessment": deduplication,
            "interpretation": (
                "Independent input-only comparisons. Catalogue idempotency is retained existing "
                "behavior; the duplicate is a counterfactual, not a historical production run. "
                "All primary source bytes and neighboring context remain. Output usage and "
                "semantic quality are unmeasured; no runtime window policy is changed."
            ),
        },
    )


if __name__ == "__main__":
    main()
