"""Office's Python wrapper must remain the launch authority for conversion workers."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform == "win32", reason="macOS uses an inherited POSIX parent pipe")
def test_parent_pipe_death_reaps_worker_group(tmp_path):
    task = tmp_path / "task"
    task.mkdir()
    pids = tmp_path / "pids.json"
    child = (
        "import os,sys,subprocess,json,time; from pathlib import Path; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        f"Path({str(pids)!r}).write_text(json.dumps([os.getpid(),p.pid])); "
        "time.sleep(60)"
    )
    parent_code = (
        "import sys; from pathlib import Path; "
        "from openkb.office.processes import run_supervised; "
        "from openkb.office.policy import office_environment; "
        "python=Path(sys.executable); sys.platform='darwin'; "
        "task=Path(sys.argv[1]); "
        "run_supervised(python.parent,python.name,None,task,"
        "{'command':[str(python),'-c',sys.argv[2]],'timeout':60},"
        "office_environment(task),lambda:None)"
    )
    with (tmp_path / "parent.log").open("wb") as log:
        parent = subprocess.Popen(
            [sys.executable, "-c", parent_code, str(task), child], stdout=log, stderr=log
        )
        owned = []
        try:
            deadline = time.monotonic() + 5
            while not pids.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert pids.exists(), (tmp_path / "parent.log").read_text()
            owned = json.loads(pids.read_text())
            parent.kill()
            parent.wait(timeout=5)
            deadline = time.monotonic() + 5
            while task.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert not task.exists(), "Supervisor did not observe its original parent's pipe EOF"
            for pid in owned:
                with pytest.raises(ProcessLookupError):
                    os.kill(pid, 0)
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=5)
            for pid in owned:
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass


def test_worker_starts_when_embedded_python_reports_a_directory(tmp_path):
    from openkb.office import processes

    supervisor = tmp_path / "supervisor.py"
    supervisor.write_bytes(Path(processes.__file__).with_name("supervisor.py.txt").read_bytes())
    (tmp_path / "worker.py").write_text("print('private worker started')\n", encoding="utf-8")
    identity = None
    if sys.platform == "linux":
        identity = Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()[19]
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "worker_python": sys.executable,
                "timeout": 5,
                "parent_pid": os.getpid(),
                "parent_identity": identity,
            }
        ),
        encoding="utf-8",
    )
    # LibreOffice's Windows wrapper starts Python with its home directory as
    # argv[0]. Reproduce an unusable sys.executable without requiring Office.
    bootstrap = (
        "import runpy,sys; "
        "sys.executable=sys.argv[3]; "
        "sys.argv=sys.argv[1:3]; "
        "runpy.run_path(sys.argv[0],run_name='__main__')"
    )
    result = subprocess.run(
        [sys.executable, "-c", bootstrap, str(supervisor), str(request), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "private worker started"
