"""The actual native helper exits even when CFB input blocks and its caller disappears."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest_plugins = ("pending_fixtures",)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux FIFO and proc identity boundary")
def test_native_deadline_interrupts_a_blocked_reader(tmp_path, prepared_cfb_helper):
    source = tmp_path / "blocked.cfb"
    os.mkfifo(source)
    identity = Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()[19]
    result = subprocess.run(
        [
            str(prepared_cfb_helper),
            str(source),
            "/Object",
            str(tmp_path / "out.cfb"),
            "10000",
            "100",
            str(os.getpid()),
            identity,
        ],
        capture_output=True,
        timeout=3,
    )
    assert result.returncode == 124


@pytest.mark.skipif(sys.platform != "linux", reason="Linux FIFO and proc identity boundary")
@pytest.mark.parametrize("reap_parent", [True, False])
def test_native_watchdog_exits_after_actual_parent_death(
    tmp_path, prepared_cfb_helper, reap_parent
):
    source = tmp_path / "blocked.cfb"
    os.mkfifo(source)
    report = tmp_path / "child.txt"
    script = (
        "import os,sys,subprocess,time;from pathlib import Path;"
        "identity=Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(')',1)[1].split()[19];"
        "child=subprocess.Popen([sys.argv[1],sys.argv[2],'/Object',sys.argv[3],'10000','30000',str(os.getpid()),identity]);"
        "Path(sys.argv[4]).write_text(str(child.pid));time.sleep(30)"
    )
    with (tmp_path / "parent.log").open("wb") as log:
        parent = subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(prepared_cfb_helper),
                str(source),
                str(tmp_path / "out.cfb"),
                str(report),
            ],
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 3
            while not report.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert report.exists()
            pid = int(report.read_text())
            time.sleep(0.1)
            parent.kill()
            if reap_parent:
                parent.wait(2)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
                except FileNotFoundError:
                    break
                if state == "Z":
                    break
                time.sleep(0.02)
            else:
                os.kill(pid, 9)
                pytest.fail("Native helper survived the exit of its original parent")
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.wait()
