"""Temporary Windows CI timing probe; never part of the release source."""

import ast
import subprocess
import sys
import time
from pathlib import Path

root = Path(sys.argv[1])
tree = ast.parse((root / "tests/test_desktop_verification_dialogs.py").read_text())
code = next(
    node.value
    for node in ast.walk(tree)
    if isinstance(node, ast.Constant)
    and isinstance(node.value, str)
    and "import time\n" in node.value
)
probe = code.replace(
    "import time\n",
    "import time\nstarted = time.monotonic()\n"
    "def stamp(stage): print('[DEBUG-dialog]', stage, round(time.monotonic() - started, 3), flush=True)\n"
    "stamp('process started')\n",
    1,
)
probe = probe.replace("app = create_application()", "stamp('imports complete')\napp = create_application()\nstamp('application created')")
probe = probe.replace("parent.show()", "parent.show()\nstamp('parent visible')")
probe = probe.replace("confirmed.append(True)", "confirmed.append(True)\n    stamp('confirmed ' + str(len(confirmed)))")
subprocess.run([sys.executable, "-c", probe], cwd=root, check=True, timeout=60)
failures = 0
for index in range(5):
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_desktop_verification_dialogs.py"],
        cwd=root,
        check=False,
        timeout=60,
    )
    failures += result.returncode != 0
    print('[DEBUG-dialog]', 'original test', index + 1, result.returncode, round(time.monotonic() - started, 3), flush=True)
raise SystemExit(bool(failures))
