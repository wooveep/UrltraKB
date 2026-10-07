"""One cancellable owned-process boundary for native probes and conversions."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable


def run_supervised(
    root: Path,
    python: str,
    launcher: str | None,
    directory: Path,
    request: dict,
    environment: dict[str, str],
    check_stop: Callable[[], None],
) -> str:
    check_stop()
    for name in ("worker", "supervisor"):
        (directory / f"{name}.py").write_bytes(
            Path(__file__).with_name(f"{name}.py.txt").read_bytes()
        )
    parent_identity = None
    if sys.platform == "linux":
        parent_identity = (
            Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()[19]
        )
    request = {
        **request,
        "worker_python": str((root / python).resolve()),
        "parent_pid": os.getpid(),
        "parent_identity": parent_identity,
    }
    request_path = directory / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    command = [str(root / python), "-B", str(directory / "supervisor.py"), str(request_path)]
    if launcher:
        command = [str(root / launcher), str(os.getpid()), str(request["timeout"] + 10), *command]
    started = time.monotonic()
    with (directory / "process.log").open("w+b") as log:
        process = subprocess.Popen(
            command,
            env=environment,
            cwd=directory,
            stdout=log,
            stderr=log,
            start_new_session=sys.platform == "linux",
        )
        try:
            while process.poll() is None:
                check_stop()
                if time.monotonic() - started > request["timeout"] + 35:
                    raise TimeoutError("Office supervisor exceeded its cleanup deadline")
                time.sleep(0.05)
        finally:
            (directory / "cancel").touch()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # The supervisor handles TERM by reaping its group. Windows' Job
                # also owns all descendants if the launcher must be terminated.
                process.terminate()
                process.wait(timeout=5)
        failure = directory / "failure.json"
        if failure.exists():
            message = json.loads(failure.read_text("utf-8"))
            if not isinstance(message, dict) or not isinstance(message.get("error"), str):
                raise ValueError("Invalid Office process failure response")
            raise ValueError(message["error"])
        log.seek(0)
        output = log.read().decode(errors="replace")
        if process.returncode:
            raise ValueError("Office process failed: " + output[-4000:])
        return output
