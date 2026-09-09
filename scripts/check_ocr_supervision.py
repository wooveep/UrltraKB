"""Check actual OCR process ownership with bounded synthetic child processes.

Run using the locked optional runtime on Windows/Linux. These probes do not
load a model or measure recognition quality. Per probe: execution 1 second,
cleanup 3 seconds, outer timeout 10 seconds, RSS 64 MiB and output 10 KB.
"""

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import psutil

root = Path(__file__).resolve().parents[1]
results = []
worker_code = """import json, os, subprocess, sys, time
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
output = Path(plan["output"])
(output / "worker.pid").write_text(str(os.getpid()), encoding="ascii")
while not (output / "start").exists(): time.sleep(0.01)
mode = plan["mode"]
if mode == "orphan":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (output / "descendant.pid").write_text(str(child.pid))
    os._exit(0)
if mode == "memory": data = bytearray(192 * 1024 * 1024)
if mode == "disk": (output / "large.bin").write_bytes(b"x" * 100000)
if mode == "stop": (output / "stop").touch()
time.sleep(60)
"""
for mode, expected in (
    ("orphan", None),
    ("time", "ocr_time_budget_exhausted"),
    ("memory", "ocr_memory_budget_exhausted"),
    ("disk", "ocr_disk_budget_exhausted"),
    ("stop", "ocr_stopped"),
):
    with tempfile.TemporaryDirectory(prefix="openkb-ocr-control-") as temporary:
        directory = Path(temporary)
        worker = directory / "worker.py"
        worker.write_text(worker_code)
        output = directory / "output"
        plan = {
            "worker": str(worker),
            "worker_sha256": hashlib.sha256(worker.read_bytes()).hexdigest(),
            "output": str(output),
            "mode": mode,
            "resources": {
                "seconds": 1,
                "cleanup_seconds": 3,
                "memory_bytes": 64 * 1024**2,
                "output_bytes": 10000,
            },
        }
        source = directory / "plan.json"
        source.write_text(json.dumps(plan))
        subprocess.run(
            [sys.executable, "-I", str(root / "openkb/ocr/supervisor.py"), str(source)],
            check=True,
            timeout=10,
        )
        result = json.loads((output / "supervision.json").read_text())
        assert result["reason"] == expected, (mode, result)
        assert result["reaped"]
        if (output / "descendant.pid").exists():
            pid = int((output / "descendant.pid").read_text())
            assert (
                not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
            )
        results.append({"probe": mode, **result})
print(json.dumps(results, indent=2))
