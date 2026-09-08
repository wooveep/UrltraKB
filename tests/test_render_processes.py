"""Real helper lifecycle checks; run after preparing desktop assets on each target."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "openkb/rendering/assets"


@pytest.mark.parametrize("timezone", ["UTC", "Asia/Shanghai"])
@pytest.mark.parametrize("case", ["gantt-basic", "gantt-cn"])
def test_gantt_dates_align_with_task_start_in_every_host_timezone(timezone, case):
    executable = ASSETS / ("renderer.exe" if os.name == "nt" else "renderer")
    if not executable.is_file():
        pytest.skip("Prepare the pinned desktop rendering assets first")
    corpus = json.loads(
        (Path(__file__).parent / "fixtures/native-render-corpus.json").read_text(encoding="utf-8")
    )
    sample = next(row for row in corpus if row["id"] == case)
    request = {
        "kind": "mermaid",
        "source": sample["markdown"].removeprefix("```mermaid\n").removesuffix("```"),
        "svg_only": True,
        "fonts": [str(ASSETS / "fonts" / f"NotoSansCJKsc-{w}.otf") for w in ("Regular", "Bold")],
    }
    output = subprocess.check_output(
        [str(executable)],
        input=json.dumps(request).encode(),
        env=dict(os.environ, TZ=timezone),
        timeout=30,
    )
    svg = ET.fromstring(json.loads(output)["svg"])
    first_tick = next(node for node in svg.iter() if node.get("class") == "tick")
    assert "".join(first_tick.itertext()) == "09-01"
    # The September 1 tick belongs at the start of the September 1 task,
    # including on Windows where Chrono ignores the TZ environment variable.
    assert first_tick.get("transform") == "translate(0.5,0)"


def test_real_formula_keeps_protocol_error_detail(tmp_path):
    from openkb.rendering.renderer import Renderer

    extension = ".exe" if os.name == "nt" else ""
    if not (ASSETS / f"renderer{extension}").is_file():
        pytest.skip("Prepare the pinned desktop rendering assets first")
    renderer = Renderer(tmp_path, ASSETS)
    try:
        valid = renderer.render(r"\frac{a}{b}", "math")
        assert valid.error is None and valid.image and Path(valid.image).is_file()
        invalid = renderer.render(r"\frac{", "math")
        assert invalid.error and "Missing close brace" in invalid.error
    finally:
        renderer.close()


@pytest.mark.parametrize("kind", ["math", "native"])
@pytest.mark.parametrize("reason", ["owner_exit", "deadline"])
def test_helper_stops_without_cooperation_from_render_thread(kind, reason):
    extension = ".exe" if os.name == "nt" else ""
    executable = ASSETS / (("node" if kind == "math" else "renderer") + extension)
    if not executable.exists():
        pytest.skip("Prepare the pinned desktop rendering assets first")
    command = [str(executable)]
    if kind == "math":
        # Exercise the product watchdog while the JavaScript main thread cannot
        # handle callbacks, stdin EOF or cooperative cancellation.
        guard = (ASSETS / "process_guard.mjs").as_uri()
        command += [
            "--input-type=module",
            "-e",
            f"import {{startParentGuard}} from '{guard}'; "
            "startParentGuard(); console.log('guard ready'); while(true) {}",
        ]
    with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]) as owner:
        environment = dict(os.environ)
        environment["OPENKB_RENDER_PARENT_PID"] = str(owner.pid)
        environment["OPENKB_RENDER_TIMEOUT_MS"] = "500" if reason == "deadline" else "10000"
        if sys.platform == "linux":
            stat = Path(f"/proc/{owner.pid}/stat").read_text()
            environment["OPENKB_RENDER_PARENT_START"] = stat[stat.rfind(")") + 2 :].split()[19]
        started = time.monotonic()
        helper = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=environment
        )
        try:
            if kind == "math":
                assert helper.stdout is not None
                assert helper.stdout.readline() == b"guard ready\n"
            else:
                time.sleep(0.2)
            assert helper.poll() is None, "Helper must start before the exit trigger"
            if reason == "owner_exit":
                owner.terminate()
                owner.wait(timeout=5)
            # Keep stdin open, including after owner exit: EOF is not responsible
            # for stopping either the CPU-bound Node thread or blocked Rust reader.
            result = helper.wait(timeout=5)
            if kind == "native":
                assert result == (124 if reason == "deadline" else 125)
            else:
                assert result == (1 if os.name == "nt" else -9)
            if reason == "deadline":
                assert time.monotonic() - started >= 0.45, "Helper exited before its deadline"
                assert owner.poll() is None
        finally:
            if helper.poll() is None:
                helper.kill()
            helper.wait(timeout=5)
            if helper.stdin:
                helper.stdin.close()
            if helper.stdout:
                helper.stdout.close()
            if owner.poll() is None:
                owner.terminate()
            owner.wait(timeout=5)
