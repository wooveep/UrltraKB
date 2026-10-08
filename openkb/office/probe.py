"""Probe the private Office ABI in child processes; UNO never enters the application."""

import json
import platform
import sys
from pathlib import Path
from typing import Callable

from openkb.office.policy import office_environment
from openkb.office.processes import run_supervised
from openkb.office.workspace import office_directory

VERSION = "26.2.6.3"
BUILD_ID = "8221e31b3ac356a1623c672912a3d2b492f7e3d1"
PYTHON_VERSION = "3.12.14"


def host_conditions() -> dict[str, str]:
    machine = platform.machine().lower()
    supported = (sys.platform in {"linux", "win32"} and machine in {"x86_64", "amd64"}) or (
        sys.platform == "darwin" and machine == "arm64"
    )
    if not supported:
        raise ValueError("Office runtime requires Linux/Windows x86_64 or macOS arm64")
    details = {
        "platform": sys.platform,
        "os": platform.platform(),
        "architecture": platform.machine(),
    }
    if sys.platform == "linux":
        records = Path("/proc/cpuinfo").read_text().split("\n\n")
        required = {"cx16", "lahf_lm", "popcnt", "ssse3", "sse4_1", "sse4_2"}
        cpus = [
            set(line.split(":", 1)[1].split())
            for record in records
            for line in record.splitlines()
            if line.startswith("flags")
        ]
        if not cpus or any(not required <= flags or not {"pni", "sse3"} & flags for flags in cpus):
            raise ValueError("Office runtime requires the Linux x86-64-v2 CPU instruction set")
        details.update(cpu_minimum="x86-64-v2", libc=" ".join(platform.libc_ver()))
    return details


def probe(
    root: Path,
    python: str,
    soffice: str,
    launcher: str | None = None,
    *,
    check_stop: Callable[[], None] = lambda: None,
    timeout: int = 20,
    task_root: Path | None = None,
) -> dict[str, str]:
    details = host_conditions()
    with office_directory(task_root, prefix="openkb-office-probe-") as directory:

        def run(arguments: list[str]) -> str:
            task = directory / str(len(list(directory.iterdir())))
            task.mkdir()
            try:
                return run_supervised(
                    root,
                    python,
                    launcher,
                    task,
                    {"command": arguments, "timeout": timeout},
                    office_environment(task),
                    check_stop,
                ).strip()
            except (OSError, ValueError) as exc:
                raise ValueError(f"Office runtime native dependency probe failed: {exc}") from exc

        version = run([str(root / soffice), "--headless", "--version"])
        if VERSION not in version or BUILD_ID not in version:
            raise ValueError(f"Office runtime build mismatch: {version}")
        code = (
            "import sys,json,uno,pyuno; print(json.dumps({"
            "'python_version':'.'.join(map(str,sys.version_info[:3])),"
            "'uno':uno.__file__,'pyuno':pyuno.__file__}))"
        )
        bundled = json.loads(run([str(root / python), "-B", "-c", code]))
        if bundled["python_version"] != PYTHON_VERSION:
            raise ValueError("Office runtime Python version mismatch")
        for name in ("uno", "pyuno"):
            path = Path(bundled[name]).resolve()
            if not path.is_relative_to(root.resolve()):
                raise ValueError("Office runtime UNO came from outside the private distribution")
            bundled[name] = path.relative_to(root.resolve()).as_posix()
        details.update(bundled, office=version)
    return details
