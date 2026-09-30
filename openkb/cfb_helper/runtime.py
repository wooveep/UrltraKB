"""One owned native operation; Python retains all identity, budget and publication state."""

import hashlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from openkb.cfb_helper.records import HelperManifest
from openkb.pending.budget import BudgetWait


def helper_path():
    root = Path(__file__).parent / "assets/runtime"
    try:
        manifest = HelperManifest.model_validate_json((root / "manifest.json").read_text("utf-8"))
    except FileNotFoundError:
        raise ValueError(
            "CFB helper is unavailable; run scripts/prepare_cfb_helper.py before packaging"
        ) from None
    name = "openkb-cfb.exe" if sys.platform == "win32" else "openkb-cfb"
    if manifest.platform != sys.platform or manifest.binary != name:
        raise ValueError("CFB helper does not match the supported native build")
    binary = root / name
    if hashlib.sha256(binary.read_bytes()).hexdigest() != manifest.sha256:
        raise ValueError("CFB helper binary failed its manifest digest check")
    if (
        hashlib.sha256(Path(__file__).with_name("dependencies.json").read_bytes()).hexdigest()
        != manifest.dependencies
    ):
        raise ValueError("CFB helper dependency provenance changed")
    if hashlib.sha256((root / "source.zip").read_bytes()).hexdigest() != manifest.source_archive:
        raise ValueError("CFB helper rebuild materials changed")
    return binary


def rebuild_storage(original, storage, meter):
    """Return only a fully verified standalone compound file, within shared limits."""
    from openkb.cfb_helper.verification import verify_storage_copy

    helper = helper_path()
    budget = meter.group.budget
    limit = min(
        budget.max_object_bytes, budget.max_total_bytes - meter.group.object_bytes, meter.remaining
    )
    if limit <= 0:
        raise BudgetWait("Object byte budget exhausted")
    meter.check()
    remaining_time = (
        budget.max_discovery_seconds
        - meter.group.discovery_seconds
        - (time.monotonic() - meter.started)
    )
    parent = str(os.getpid())
    identity = (
        Path(f"/proc/{parent}/stat").read_text().rsplit(")", 1)[1].split()[19]
        if sys.platform == "linux"
        else "parent-handle"
    )
    with tempfile.TemporaryDirectory(prefix="openkb-cfb-") as temporary:
        directory = Path(temporary)
        output = directory / "restored.cfb"
        command = [
            str(helper),
            str(original),
            "/" + "/".join(storage),
            str(output),
            str(limit),
            str(max(1, int(remaining_time * 1000))),
            parent,
            identity,
        ]
        with (directory / "process.log").open("w+b") as log:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            try:
                while child.poll() is None:
                    meter.check()
                    time.sleep(0.025)
                meter.check()
            finally:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(2)
                    except subprocess.TimeoutExpired:
                        child.kill()
                child.wait()
            if child.returncode:
                if child.returncode == 124:
                    raise BudgetWait("Discovery time budget exhausted")
                log.seek(0)
                raise ValueError(
                    "requires_container_rebuild: "
                    + log.read(4000).decode("utf-8", errors="replace")
                )
        if output.stat().st_size > limit:
            raise BudgetWait("Object byte budget exhausted")
        verify_storage_copy(original, storage, output, meter)
        return output.read_bytes()
