"""Measure one fixed corpus document through shared intake, parsing and evidence reads.

This runner deliberately ends before model compilation. It verifies parsing
coverage, not semantic model quality. Each invocation owns a fresh worker tree
and requires explicit time, memory and output bounds. No credential is needed.
"""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing
import os
import platform
import re
import time
from dataclasses import asdict
from pathlib import Path


def run_case(plan):
    from openkb import config
    from openkb.application.documents import import_document
    from openkb.application.execution import ExecutionContext
    from openkb.application.knowledge_bases import initialize_kb
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.evidence import Evidence, ParseStore
    from openkb.knowledge_commit import wiki_version
    from openkb.processing import ProcessingIncomplete
    from openkb.sources import SourceStore
    from openkb.state import HashRegistry

    root = Path(plan["output"])
    config.GLOBAL_CONFIG_DIR = root / "global"
    config.GLOBAL_CONFIG_PATH = root / "global/global.yaml"
    kb = root / "kb"
    initialize_kb(
        kb,
        seed_environment=False,
        model="openai/offline-benchmark",
        api_key="synthetic-offline",
        openai_api_base="http://127.0.0.1:9/v1",
    )
    budgets = {
        "context_tokens": 8192,
        "output_tokens": 512,
        "request_timeout": 1,
        "stage_timeout": plan["seconds"],
        "document_timeout": plan["seconds"],
        "cleanup_timeout": plan["cleanup_seconds"],
        "max_attempts": 1,
        "max_requests": 1,
        "max_tokens": 10000,
        "concurrency": 1,
    }
    apply_kb_config_patch(
        kb,
        KbConfigPatchRequest(
            kb=str(kb), config={"processing": budgets, "parsing": plan.get("parsing", {})}
        ),
    )
    original = Path(plan["corpus"]) / plan["document"]["file"]
    if HashRegistry.hash_file(original) != plan["document"]["sha256"]:
        raise ValueError("Corpus input changed after planning")
    before = wiki_version(kb)
    events = []
    started = time.monotonic()

    def observe(event):
        events.append({**event, "elapsed_seconds": time.monotonic() - started})
        if event.get("stage") == "compiling":
            raise ProcessingIncomplete("benchmark_parsing_only", "parsed")

    result = import_document(kb, original, context=ExecutionContext(on_event=observe))
    found = []
    parses = ParseStore(kb)
    if result.parse_id:
        parsed = parses.load(result.parse_id)
        for fact in plan["document"]["facts"]:
            expression = re.compile(r"\s+".join(re.escape(word) for word in fact["text"].split()))
            reference = None
            for block in parsed.blocks:
                if "page" in fact and block.location.get("page") != fact["page"]:
                    continue
                start, tail = 0, ""
                while start < block.chars:
                    piece = parses.read(
                        Evidence(
                            result.source_id, result.input_version, parsed.id, block.id, start
                        ),
                        max_chars=4096,
                    )
                    text = tail + piece.text
                    match = expression.search(text)
                    if match:
                        reference = Evidence(
                            result.source_id,
                            result.input_version,
                            parsed.id,
                            block.id,
                            start - len(tail) + match.start(),
                            start - len(tail) + match.end(),
                        )
                        verified = parses.read(reference, max_chars=4096)
                        assert expression.fullmatch(verified.text)
                        break
                    tail = text[-2048:]
                    if piece.next_start is None:
                        break
                    start = piece.next_start
                if reference:
                    break
            found.append({"fact": fact, "evidence": asdict(reference) if reference else None})
        quality = parsed.quality
        blocks = len(parsed.blocks)
    else:
        quality, blocks = [], 0
    assert wiki_version(kb) == before, "Parsing-only run changed published knowledge"
    assert result.usage.get("observable_attempts", 0) == 0, "Unexpected model request"
    store = SourceStore(kb)
    original_retained = store.original(store.version(result.input_version))
    assert HashRegistry.hash_file(original_retained) == plan["document"]["sha256"]
    report = {
        "document": asdict(result),
        "events": events,
        "blocks": blocks,
        "quality": quality,
        "facts": found,
        "fact_coverage": sum(item["evidence"] is not None for item in found),
        "fact_total": len(plan["document"]["facts"]),
        "elapsed_seconds": time.monotonic() - started,
        "knowledge_unchanged": True,
        "mode": "parsing_only",
    }
    if plan.get("parsing"):
        from openkb.application.source_history import source_status

        report["local_ocr"] = source_status(kb, result.source_id)["local_ocr"]
    (root / "result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def rss(pid):
    if os.name != "nt":
        try:
            match = re.search(r"^VmRSS:\s+(\d+) kB$", Path(f"/proc/{pid}/status").read_text(), re.M)
            return int(match[1]) * 1024 if match else 0
        except FileNotFoundError:
            return 0
    import ctypes
    from ctypes import wintypes

    class Memory(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t)
            for name in (
                "peak_ws",
                "ws",
                "peak_paged",
                "paged",
                "peak_nonpaged",
                "nonpaged",
                "pagefile",
                "peak_pagefile",
            )
        ]

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    query = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
    query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Memory), wintypes.DWORD]
    handle = api.OpenProcess(0x0400 | 0x0010, False, pid)
    if not handle:
        return 0
    try:
        result = Memory()
        result.cb = ctypes.sizeof(result)
        if not query(handle, ctypes.byref(result), result.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return result.ws
    finally:
        api.CloseHandle(handle)


def main():
    from openkb.runtime.process_tree import ProcessTree, isolated_target

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    for key in ("seconds", "cleanup_seconds", "memory_bytes", "output_bytes"):
        if type(plan[key]) not in (int, float) or not math.isfinite(plan[key]) or plan[key] <= 0:
            parser.error("Every resource limit must be explicitly positive")
    root = Path(plan["output"])
    root.mkdir(parents=True, exist_ok=False)
    (root / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    process = context.Process(target=isolated_target, args=(run_case, (plan,), ready))
    process.start()
    try:
        tree = ProcessTree(process, ready=ready)
    except BaseException:
        process.terminate()
        process.join(plan["cleanup_seconds"])
        if process.is_alive():
            process.kill()
            process.join(plan["cleanup_seconds"])
        raise
    started = time.monotonic()
    peak_memory = peak_output = 0
    reason = None
    try:
        ready.set()
        while process.is_alive():
            peak_memory = max(peak_memory, rss(process.pid))
            size = 0
            for path in root.rglob("*"):
                try:
                    if path.is_file():
                        size += path.stat().st_size
                except FileNotFoundError:
                    pass  # An atomic temporary file was renamed between observations.
            peak_output = max(peak_output, size)
            if time.monotonic() - started > plan["seconds"]:
                reason = "time_budget_exhausted"
            elif peak_memory > plan["memory_bytes"]:
                reason = "memory_budget_exhausted"
            elif peak_output > plan["output_bytes"]:
                reason = "output_budget_exhausted"
            if reason:
                break
            process.join(0.05)
    finally:
        if tree.alive():
            tree.terminate()
        process.join(plan["cleanup_seconds"])
        if tree.alive():
            tree.terminate(force=True)
            process.join(plan["cleanup_seconds"])
        reaped = not tree.alive()
        tree.close()
    outcome = {
        "reason": reason,
        "exit_code": process.exitcode,
        "reaped": reaped,
        "peak_sampled_worker_rss_bytes": peak_memory,
        "peak_sampled_output_bytes": peak_output,
        "elapsed_seconds": time.monotonic() - started,
        "platform": platform.platform(),
        "python": platform.python_version(),
    }
    (root / "supervision.json").write_text(json.dumps(outcome, indent=2), encoding="utf-8")
    print(json.dumps(outcome))
    return 0 if not reason and process.exitcode == 0 and reaped else 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
