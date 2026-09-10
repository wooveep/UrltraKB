"""Frozen command output must remain readable outside a Unicode console."""

import json
import os
import subprocess
import sys
from pathlib import Path


def test_redirected_frozen_streams_use_utf8_with_legacy_system_encoding():
    hook = Path(__file__).resolve().parents[1] / "packaging/desktop/runtime_hook.py"
    program = (
        "import runpy,sys,json,click; runpy.run_path(sys.argv[1]); "
        "click.echo(json.dumps({'name': '\\u90e8\\u7f72\\u624b\\u518c'}, ensure_ascii=False)); "
        "click.echo('\\u5904\\u7406\\u5b8c\\u6210', err=True)"
    )
    result = subprocess.run(
        [sys.executable, "-c", program, str(hook)],
        env={**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"},
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode("ascii", errors="replace")
    assert json.loads(result.stdout.decode("utf-8")) == {"name": "部署手册"}
    assert result.stderr.decode("utf-8").strip() == "处理完成"
