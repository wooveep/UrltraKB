"""Run the real OS-lock acceptance probe on either portable target.

Only temporary knowledge bases are written. This is intentionally separate
from the fast regression suite: the accepted gate requires >15s contention.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import platform
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openkb.locks import LockCancelled, atomic_write_text, kb_ingest_lock  # noqa: E402


def hold(kb, ready, release):
    with kb_ingest_lock(Path(kb) / ".openkb"):
        ready.set()
        if not release.wait(60):
            raise TimeoutError("Probe controller did not release holder")


def write(kb, waiting, cancelled, result):
    try:
        with kb_ingest_lock(Path(kb) / ".openkb", on_wait=waiting.set, cancelled=cancelled.is_set):
            atomic_write_text(Path(kb) / "written.md", "committed")
        result.send("committed")
    except LockCancelled:
        result.send("cancelled")
    finally:
        result.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hold-seconds", type=float, default=16)
    args = parser.parse_args()
    ctx = mp.get_context("spawn")
    processes = []
    release = ctx.Event()
    with tempfile.TemporaryDirectory(prefix="openkb-lock-acceptance-") as directory:
        root = Path(directory)
        kb, other = root / "kb", root / "other"
        kb.mkdir()
        other.mkdir()
        ready = ctx.Event()
        holder = ctx.Process(target=hold, args=(kb, ready, release))
        processes.append(holder)
        holder.start()
        try:
            assert ready.wait(15), "holder never acquired"
            results = []
            waits = []
            stops = []
            for target in (kb, kb / ".", other):
                parent, child = ctx.Pipe(duplex=False)
                waiting, cancelled = ctx.Event(), ctx.Event()
                process = ctx.Process(target=write, args=(target, waiting, cancelled, child))
                processes.append(process)
                process.start()
                child.close()
                results.append(parent)
                waits.append(waiting)
                stops.append(cancelled)
            assert waits[0].wait(15) and waits[1].wait(15), "contention was not observed"
            assert results[2].poll(15) and results[2].recv() == "committed"
            stops[1].set()
            assert results[1].poll(15) and results[1].recv() == "cancelled"
            # Keep a real contender blocked past the Windows legacy 10s limit.
            started = time.monotonic()
            time.sleep(args.hold_seconds)
            assert not results[0].poll(), "same-KB writer entered before release"
            assert not (kb / "written.md").exists()
            release.set()
            assert results[0].poll(15) and results[0].recv() == "committed"
            elapsed = time.monotonic() - started
            for process in processes:
                process.join(15)
                assert not process.is_alive() and process.exitcode == 0
            assert (kb / "written.md").read_text() == "committed"
            print(
                json.dumps(
                    {
                        "platform": platform.platform(),
                        "python": platform.python_version(),
                        "same_kb_wait_seconds": round(elapsed, 2),
                        "cancelled_waiter": "passed",
                        "independent_kb": "passed",
                        "process_reaping": "passed",
                    }
                )
            )
        finally:
            release.set()
            for process in processes:
                process.join(2)
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            for connection in locals().get("results", []):
                connection.close()


if __name__ == "__main__":
    mp.freeze_support()
    main()
