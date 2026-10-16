"""Verify a retained PDF on a KB copy with real model calls and two safe interruptions.

Use --live to authorize provider calls. Existing KB data is only read and hashed.
Fault injection happens after atomic checkpoint saves, using production use cases.
"""

import argparse
import hashlib
import json
import logging
import shutil
import time
import uuid
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch


def file_hashes(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file() and not p.is_symlink()
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb", required=True, type=Path)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--phase", choices=("index", "summary", "complete"), required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required for this real-provider verification")
    import litellm

    litellm.suppress_debug_info = True
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.source_retry import retry_source
    from ultrakb.compile_checkpoints import CompileCheckpoints
    from ultrakb.config import resolve_credential_bundle
    from ultrakb.execution_capacity import request_input
    from ultrakb.index_stages import IndexStages
    from ultrakb.llm_execution import CompletionExecutor
    from ultrakb.locks import LockCancelled, atomic_write_json, kb_ingest_lock, kb_read_lock
    from ultrakb.model_log_secrets import redact_model_logs
    from ultrakb.request_budget import input_limit

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.disable(logging.DEBUG)
    original = args.kb.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    kb = output / "kb"
    baseline = output / "original-files.json"
    if not baseline.exists():
        with kb_read_lock(original / ".ultrakb"):
            atomic_write_json(baseline, file_hashes(original))
            shutil.copytree(original, kb, symlinks=True)
    elif not kb.is_dir():
        raise RuntimeError("Verification copy is missing")
    # A filesystem copy has a distinct lifecycle identity. Rebind only the
    # verification copy's mutable task ownership; keep every paid count and ID.
    from ultrakb.lifecycle import current_generation

    with kb_ingest_lock(kb / ".ultrakb"):
        generation = current_generation(kb)
        for path in (kb / ".ultrakb/model-tasks").glob("*.json"):
            task = json.loads(path.read_text())
            if task["kb_generation"] != generation:
                task["kb_generation"] = generation
                atomic_write_json(path, task)
    before_ids = {p.stem for p in (kb / ".ultrakb/usage/requests").glob("*.json")}
    task_id = uuid.uuid4().hex
    for kind in ("result", "requests"):
        previous = output / f"{args.phase}-{kind}.json"
        if previous.exists():
            previous.replace(output / f"{args.phase}-{kind}-previous-{task_id[:8]}.json")
    atomic_write_json(output / f"{args.phase}-requests.json", [])
    context = ExecutionContext(usage_task_id=task_id)
    started = time.monotonic()
    injection = {"triggered": False, "index_nodes": 0}
    measured = []
    save_index, save_compile, kwargs = (
        IndexStages.save,
        CompileCheckpoints.save,
        CompletionExecutor._kwargs,
    )

    def index_saved(self, key, value):
        save_index(self, key, value)
        if key.startswith("stage:node-summary:"):
            injection["index_nodes"] += 1
            if args.phase == "index" and injection["index_nodes"] == 5:
                injection["triggered"] = True
                context.executor.cancel()

    def compile_saved(self, key, signature, value, **options):
        save_compile(self, key, signature, value, **options)
        if args.phase == "summary" and key.startswith("summary:") and not injection["triggered"]:
            injection["triggered"] = True
            context.executor.cancel()

    def prepared(self, role, request):
        sdk = kwargs(self, role, request)
        measure = self.request_measure(role)
        if measure:
            measured.append(
                {
                    "role": role,
                    "stage": request.stage,
                    "input_units": measure.payload_tokens(request_input(sdk)),
                    "input_limit": input_limit(measure, sdk),
                    "unit": measure.unit,
                    "max_tokens": sdk.get("max_tokens", sdk.get("max_completion_tokens")),
                }
            )
            atomic_write_json(output / (args.phase + "-requests.json"), measured)
        return sdk

    def event(value):
        if value.get("stage") in {"indexed", "compiling", "committed", "converting"}:
            print("EVENT", json.dumps(value, ensure_ascii=False), flush=True)

    context.on_event = event
    result, error = None, None
    with (
        patch.object(IndexStages, "save", index_saved),
        patch.object(CompileCheckpoints, "save", compile_saved),
        patch.object(CompletionExecutor, "_kwargs", prepared),
        redact_model_logs(resolve_credential_bundle(kb)),
    ):
        try:
            result = retry_source(kb, args.source, context=context, retry_failed_response=True)
        except LockCancelled:
            error = "LockCancelled"
        except Exception as failure:
            error = type(failure).__name__ + ": " + str(failure)
    requests = []
    for path in (kb / ".ultrakb/usage/requests").glob("*.json"):
        if path.stem not in before_ids:
            record = json.loads(path.read_text())
            if record.get("source_id") == args.source:
                requests.append(record)
    after = file_hashes(original)
    expected = json.loads(baseline.read_text())
    changed = [name for name in set(after) | set(expected) if after.get(name) != expected.get(name)]
    evidence = {
        "phase": args.phase,
        "usage_task_id": task_id,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "injection": injection,
        "error": error,
        "result": asdict(result) if result else None,
        "stages": dict(Counter(r["stage"] for r in requests)),
        "sent": sum(r.get("send_status") == "sent" for r in requests),
        "input_tokens": sum(r.get("input_total") or 0 for r in requests),
        "output_tokens": sum(r.get("output_total") or 0 for r in requests),
        "usage_unknown": sum(r.get("usage_status") == "unknown" for r in requests),
        "task_budget": context.executor.budget.snapshot() if context.executor else None,
        "all_measured_requests_fit": all(r["input_units"] <= r["input_limit"] for r in measured),
        "original_files_changed": changed,
    }
    atomic_write_json(output / (args.phase + "-result.json"), evidence)
    print(json.dumps(evidence, ensure_ascii=False, indent=2), flush=True)
    if changed:
        raise AssertionError("Original KB changed during verification")
    if error not in {None, "LockCancelled"}:
        raise SystemExit(1)
    if args.phase != "complete" and not injection["triggered"]:
        raise AssertionError("Required checkpoint interruption was not reached")
    if args.phase == "complete" and (result is None or result.status != "added"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
