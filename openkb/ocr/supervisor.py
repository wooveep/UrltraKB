"""Optional-runtime process supervisor; no application or Wiki write interface."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil


def supervise(plan: dict, plan_path: Path) -> dict:
    output = Path(plan["output"])
    output.mkdir(parents=True, exist_ok=True)
    worker = Path(plan["worker"])
    if hashlib.sha256(worker.read_bytes()).hexdigest() != plan["worker_sha256"]:
        raise ValueError("OCR worker changed after admission")
    limits = plan["resources"]
    for key in ("seconds", "cleanup_seconds", "memory_bytes", "output_bytes"):
        value = limits[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (float, int))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError("OCR supervisor limits must be positive")
    owner = psutil.Process().parent()
    if sys.platform == "linux":
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            raise OSError("Cannot own orphaned OCR descendants")
    started = time.monotonic()
    peak_memory = peak_disk = 0
    reason = None
    descendants: dict[int, psutil.Process] = {}
    job = None
    with (output / "worker.log").open("wb") as log:
        # Inherit the application worker's process group / Windows Job Object.
        # Its owner can still reap the entire tree if this supervisor fails.
        process = subprocess.Popen(
            [sys.executable, "-I", str(worker), str(plan_path)],
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=0x00000004 if os.name == "nt" else 0,  # CREATE_SUSPENDED
        )
        child = psutil.Process(process.pid)
        try:
            if os.name == "nt":
                helper = Path(__file__).parent.parent / "runtime/process_tree.py"
                spec = importlib.util.spec_from_file_location("ocr_process_tree", helper)
                if spec is None or spec.loader is None:
                    raise RuntimeError("Missing Windows OCR process owner")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                job = module._WindowsJob(process.pid)
                module.resume_suspended_process(process.pid)
            # Worker imports and inference begin only after ownership attaches.
            (output / "start").touch()
            while process.poll() is None:
                current = psutil.Process().children(recursive=True)
                descendants.update((p.pid, p) for p in current)
                memory = sum(p.memory_info().rss for p in current if p.is_running())
                disk = sum(p.stat().st_size for p in output.rglob("*") if p.is_file())
                peak_memory, peak_disk = max(peak_memory, memory), max(peak_disk, disk)
                if (output / "stop").exists() or owner is None or not owner.is_running():
                    reason = "ocr_stopped"
                elif time.monotonic() - started >= limits["seconds"]:
                    reason = "ocr_time_budget_exhausted"
                elif memory > limits["memory_bytes"]:
                    reason = "ocr_memory_budget_exhausted"
                elif disk > limits["output_bytes"]:
                    reason = "ocr_disk_budget_exhausted"
                if reason:
                    break
                time.sleep(0.05)
        except psutil.NoSuchProcess:
            pass
        finally:
            deadline = time.monotonic() + limits["cleanup_seconds"]
            try:
                descendants.update((p.pid, p) for p in psutil.Process().children(recursive=True))
            except psutil.NoSuchProcess:
                pass
            descendants[child.pid] = child
            if job is not None:
                job.terminate()
            for member in reversed(list(descendants.values())):
                try:
                    member.kill()
                except psutil.NoSuchProcess:
                    pass
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                reason = "ocr_cleanup_unconfirmed"
            _, alive = psutil.wait_procs(
                [p for p in descendants.values() if p.pid != process.pid],
                timeout=max(0, deadline - time.monotonic()),
            )
            alive = [p for p in alive if p.is_running() and p.status() != psutil.STATUS_ZOMBIE]
            if alive:
                reason = "ocr_cleanup_unconfirmed"
            # Linux adopts descendants whose immediate parent exited between
            # observations. Re-scan after killing known parents, within the same
            # cleanup deadline, so fast fork-and-exit cannot escape ownership.
            while True:
                members = psutil.Process().children(recursive=True)
                if not members:
                    break
                for member in members:
                    try:
                        member.kill()
                    except psutil.NoSuchProcess:
                        pass
                _, alive = psutil.wait_procs(members, timeout=max(0, deadline - time.monotonic()))
                if time.monotonic() >= deadline:
                    if alive:
                        reason = "ocr_cleanup_unconfirmed"
                    break
            if job is not None:
                if job.active():
                    reason = "ocr_cleanup_unconfirmed"
                job.close()
    return {
        "reason": reason,
        "exit_code": process.returncode,
        "reaped": reason != "ocr_cleanup_unconfirmed",
        "elapsed_seconds": time.monotonic() - started,
        "peak_memory_bytes": peak_memory,
        "peak_output_bytes": peak_disk,
    }


if __name__ == "__main__":
    path = Path(sys.argv[1]).resolve()
    plan = json.loads(path.read_text(encoding="utf-8"))
    result = supervise(plan, path)
    output = Path(plan["output"])
    temporary = output / "supervision.json.tmp"
    temporary.write_text(json.dumps(result), encoding="utf-8")
    temporary.replace(output / "supervision.json")
