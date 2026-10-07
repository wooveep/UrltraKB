"""Office's Python wrapper must remain the launch authority for conversion workers."""

import json
import os
import subprocess
import sys
from pathlib import Path


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
