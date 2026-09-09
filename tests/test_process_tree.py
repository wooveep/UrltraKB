"""Real external-runtime descendants remain owned after their worker exits."""

import multiprocessing as mp
import subprocess
import sys
import time
from pathlib import Path

from openkb.runtime.process_tree import ProcessTree, isolated_target


def launch_descendant(marker: str) -> None:
    subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
    Path(marker).write_text("child started")


def test_process_tree_reclaims_descendant_after_root_exits(tmp_path):
    context = mp.get_context("spawn")
    ready = context.Event()
    marker = tmp_path / "started"
    process = context.Process(
        target=isolated_target, args=(launch_descendant, (str(marker),), ready)
    )
    process.start()
    tree = ProcessTree(process)
    try:
        ready.set()
        process.join(10)
        assert not process.is_alive() and marker.exists()
        assert tree.alive()
        tree.terminate(force=True)
        deadline = time.monotonic() + 5
        while tree.alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not tree.alive()
    finally:
        tree.terminate(force=True)
        process.join(5)
        tree.close()
        process.close()
