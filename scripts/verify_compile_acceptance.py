"""Recover actual saved compiler responses on an isolated KB copy, with no network.

No model output is fabricated. Directory ownership is relocated for the copy;
request digests, original bytes, page bases, configuration and paid counts stay fixed.
"""

import argparse
import hashlib
import json
import logging
import shutil
import socket
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch


def hashes(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file() and not p.is_symlink()
    }


def relocate_ownership(kb):
    """A copy has a new directory identity; never relax production identity checks."""
    from ultrakb.compile_checkpoints import digest
    from ultrakb.lifecycle import current_generation
    from ultrakb.locks import atomic_write_json

    generation = current_generation(kb)
    for path in (kb / ".ultrakb/model-tasks").glob("*.json"):
        value = json.loads(path.read_text())
        value["kb_generation"] = generation
        atomic_write_json(path, value)
    for path in list((kb / ".ultrakb/compile-checkpoints").glob("*/*/manifest.json")):
        value = json.loads(path.read_text())
        if "kb_generation" not in value["identity"]:
            continue
        old_key = path.parent.name
        value["identity"]["kb_generation"] = generation
        new_key = digest(value["identity"])
        task = path.parent.parent.name
        old_responses = kb / ".ultrakb/compile-responses" / task / old_key
        new_responses = old_responses.with_name(new_key)
        if old_responses.is_dir() and old_responses != new_responses:
            old_responses.rename(new_responses)
            for record in new_responses.glob("*.json"):
                data = json.loads(record.read_text())
                if "response_file" in data:
                    data["response_file"] = data["response_file"].replace(old_key, new_key)
                    atomic_write_json(record, data)
        for entry in value["entries"].values():
            for field in ("response_failures", "received_responses", "delivery_resolutions"):
                if field in entry:
                    entry[field] = [p.replace(old_key, new_key) for p in entry[field]]
            if "latest_response" in entry:
                entry["latest_response"] = entry["latest_response"].replace(old_key, new_key)
        atomic_write_json(path, value)
        if path.parent.name != new_key:
            path.parent.rename(path.parent.with_name(new_key))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb", type=Path, required=True)
    parser.add_argument("--model-task", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    original, output = args.kb.resolve(), args.output.resolve()
    if output.is_relative_to(original) or output.exists():
        raise ValueError("Use a fresh output directory outside the original KB")
    before_original = hashes(original)
    output.mkdir(parents=True)
    kb = output / "kb"
    shutil.copytree(original, kb, symlinks=True)
    logging.disable(logging.CRITICAL)
    denied = []

    def deny(*a, **kw):
        denied.append("model_or_network_send")
        raise AssertionError("Saved-response acceptance verification forbids every model send")

    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = deny
    import litellm

    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.source_retry import retry_source
    from ultrakb.compile_checkpoints import CompileCheckpoints
    from ultrakb.locks import atomic_write_json, kb_ingest_lock
    from ultrakb.model_task import read_task

    with kb_ingest_lock(kb / ".ultrakb"):
        relocate_ownership(kb)
    task = read_task(kb, args.model_task)
    [source_id] = task.inputs
    manifests = list(
        (kb / ".ultrakb/compile-checkpoints" / args.model_task).glob("*/manifest.json")
    )
    [manifest] = [json.loads(p.read_text()) for p in manifests]
    accepted = {
        key: entry for key, entry in manifest["entries"].items() if entry.get("state") == "accepted"
    }
    failures = {
        key: entry
        for key, entry in manifest["entries"].items()
        if entry.get("response_failures") and entry.get("state") != "accepted"
    }
    protected = {
        name: value
        for name, value in hashes(kb).items()
        if name.startswith((".ultrakb/index-checkpoints/", ".ultrakb/compile-responses/"))
    }
    loads, replays, saved = [], [], []
    load, replay, save = (
        CompileCheckpoints.load,
        CompileCheckpoints.retained_response,
        CompileCheckpoints.save,
    )

    def observe_load(self, key, signature):
        value = load(self, key, signature)
        if value is not None and key in accepted:
            assert signature == accepted[key]["request_digest"]
            loads.append(key)
        return value

    def observe_replay(self, key, signature, **options):
        value = replay(self, key, signature, **options)
        if value is not None:
            assert signature == failures[key]["request_digest"]
            replays.append({"key": key, "request_digest": signature, **value[1]})
        return value

    def observe_save(self, key, signature, result, **options):
        save(self, key, signature, result, **options)
        if key in failures:
            entry = json.loads(self.path.read_text())["entries"][key]
            assert entry["state"] == "accepted" and entry["repair_used"]
            assert entry["response_failures"] == failures[key]["response_failures"]
            assert entry["reused_response"]["response_sha256"]
            saved.append(key)

    context = ExecutionContext()
    with (
        patch.object(litellm, "completion", deny),
        patch.object(litellm, "acompletion", deny),
        patch.object(CompileCheckpoints, "load", observe_load),
        patch.object(CompileCheckpoints, "retained_response", observe_replay),
        patch.object(CompileCheckpoints, "save", observe_save),
    ):
        result = retry_source(kb, source_id, context=context)
    after = hashes(kb)
    assert hashes(original) == before_original, "Original KB changed"
    assert all(after.get(name) == value for name, value in protected.items()), (
        "Frozen evidence changed"
    )
    assert set(saved) == set(failures), "Not every retained failed page was accepted"
    assert set(loads) >= set(accepted), "Accepted knowledge was regenerated or skipped"
    assert not denied
    assert context.model_task.record.used == task.used
    assert context.model_task.record.stages == task.stages
    assert result.status == "added", result.message
    report = {
        "result": asdict(result),
        "model_sends": 0,
        "used_before": task.used,
        "used_after": context.model_task.record.used,
        "stages": task.stages,
        "accepted_reused": sorted(set(loads)),
        "responses_reaccepted": replays,
        "reused_by_kind": dict(Counter(key.split(":", 1)[0] for key in set(loads))),
        "protected_files": len(protected),
        "original_files_unchanged": len(before_original),
        "relocation": "Only copied directory ownership and response paths were rebound",
    }
    atomic_write_json(output / "verification.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
