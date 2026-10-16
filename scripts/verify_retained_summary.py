"""Replay one retained summary through real delivery checks without sends or KB writes.

The accepted batches and request bytes are read from their existing checkpoints.
Only the explicitly selected captured response may supply the next batch; later
batches are left unprocessed. Evidence and candidate output go outside the KB.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import socket
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def deny_network(*args, **kwargs):
    raise AssertionError("Real network/model calls are forbidden during retained-summary replay")


def protected_files(kb, checkpoint, model_task):
    paths = [*checkpoint.parent.glob("*.json"), model_task]
    for root in (kb / ".ultrakb/index-checkpoints", kb / ".ultrakb/compile-responses"):
        paths.extend(p for p in root.rglob("*") if p.is_file())
    return {p: sha(p) for p in paths}


async def replay(kb, checkpoint, response_record, output):
    import yaml

    from ultrakb.agent.document_summary import (
        SYSTEM_TEMPLATE,
        build_document_summary,
        summary_capacity,
    )
    from ultrakb.agent.page_delivery import validate_page_delivery
    from ultrakb.agent.page_response import parse_summary
    from ultrakb.agent.source_excerpts import SourceExcerpts
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.compile_checkpoints import digest

    manifest = json.loads(checkpoint.read_text())
    diagnostic = json.loads(response_record.read_text())
    task_id = manifest["task_id"]
    assert diagnostic["task_id"] == task_id
    source_response = (kb / diagnostic["response_file"]).resolve()
    assert source_response.is_relative_to(kb)
    assert sha(source_response) == diagnostic["response_sha256"]
    raw = source_response.read_text()
    budget_path = kb / ".ultrakb/model-tasks" / f"{task_id}.json"
    before = protected_files(kb, checkpoint, budget_path)
    budget = json.loads(budget_path.read_text())
    approved = {}
    for path in checkpoint.parent.glob("payload-*.json"):
        payload = json.loads(path.read_text())
        entry = manifest["entries"].get(payload["key"], {})
        if entry.get("state") == "accepted" and digest(payload) == entry.get("payload_digest"):
            approved[payload["key"]] = payload["result"]
    events, resolutions = [], []
    candidate = None

    class Finished(Exception):
        pass

    class ReadOnlyCheckpoints:
        request_version = 1
        structured_summary = False  # This incident owns the retained v1 request bytes.

        def load(self, key, request_digest):
            entry = manifest["entries"].get(key)
            if entry:
                assert entry["request_digest"] == request_digest, f"Retained request changed: {key}"
                if key in approved:
                    events.append({"event": "reused", "key": key, "request_digest": request_digest})
                    return approved[key]
            return None

        def failure_code(self, key, request_digest):
            entry = manifest["entries"].get(key, {})
            return (
                entry.get("failure_code") if entry.get("request_digest") == request_digest else None
            )

        def claim_generation(self, key, request_digest, **kwargs):
            if key != diagnostic["checkpoint_key"]:
                raise Finished
            assert request_digest == diagnostic["request_digest"]

        def save(self, key, request_digest, result, **kwargs):
            nonlocal candidate
            assert key == diagnostic["checkpoint_key"]
            candidate = result
            events.append(
                {"event": "validated_in_memory", "key": key, "request_digest": request_digest}
            )

    async def generate(messages, stage, originals, sections):
        assert stage == diagnostic["stage"]
        assert digest(messages) == diagnostic["request_digest"]
        excerpts = SourceExcerpts(originals)
        validated = validate_page_delivery(
            raw,
            response_protocol=diagnostic["response_protocol"],
            expected_sections=sections,
            original_excerpts=excerpts,
        )
        resolutions.extend(excerpts.resolutions)
        return parse_summary(validated, sections)

    doc_name = manifest["identity"]["document"]
    # Select the retained source revision from the checkpoint's own frozen identities.
    revisions = manifest["conditions"]["revisions"]
    unit = next(item for item in revisions if item["unit"]["doc_name"] == doc_name)
    source_id = unit["unit"]["source_id"]
    unit_revision = unit["revision"]["unit_revision_id"]
    wiki = kb / ".ultrakb/index-checkpoints" / source_id / unit_revision / "data/wiki"
    cfg = yaml.safe_load((kb / ".ultrakb/config.yaml").read_text())
    cfg["model"] = manifest["conditions"]["bindings"]["models"]["compile"]
    windows = CompileSourceWindows(wiki, doc_name, kb_dir=kb)
    fits = summary_capacity(
        cfg, custom_endpoint=True, legacy_format=True, base_url="https://api.deepseek.com"
    )
    try:
        await build_document_summary(
            windows,
            system_message={
                "role": "system",
                "content": SYSTEM_TEMPLATE.format(language=cfg.get("language", "en")),
            },
            generate=generate,
            fits=fits,
            checkpoints=ReadOnlyCheckpoints(),
        )
    except Finished:
        pass
    finally:
        assert protected_files(kb, checkpoint, budget_path) == before, "Protected KB files changed"
    assert candidate is not None, "The selected response was not validated"
    output.mkdir(parents=True, exist_ok=True)
    (output / "candidate.json").write_text(json.dumps(candidate, ensure_ascii=False, indent=2))
    report = {
        "status": "validated_offline",
        "task_id": task_id,
        "checkpoint": diagnostic["checkpoint_key"],
        "same_request_digest": diagnostic["request_digest"],
        "events": events,
        "resolution_count": len(resolutions),
        "resolutions": resolutions,
        "source_response_sha256": sha(source_response),
        "protected_files_unchanged": len(before),
        "model_calls": 0,
        "budget_before": budget["used"],
        "budget_after": json.loads(budget_path.read_text())["used"],
        "budget_limit": budget["policy"]["max_calls"],
        "published": False,
    }
    (output / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "resolutions"},
            ensure_ascii=False,
            indent=2,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--response-record", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    kb, checkpoint, response_record, output = [
        p.expanduser().resolve()
        for p in (args.kb, args.checkpoint, args.response_record, args.output)
    ]
    if not checkpoint.is_relative_to(kb) or not response_record.is_relative_to(kb):
        parser.error("Checkpoint and response record must belong to the selected KB")
    if output.is_relative_to(kb) or any(
        (parent / ".ultrakb/config.yaml").is_file() for parent in [output, *output.parents]
    ):
        parser.error("Replay evidence must be saved outside any KB")
    logging.disable(logging.CRITICAL)
    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    socket.create_connection = deny_network
    asyncio.run(replay(kb, checkpoint, response_record, output))


if __name__ == "__main__":
    main()
