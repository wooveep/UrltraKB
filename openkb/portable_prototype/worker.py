"""Top-level spawn target: all heavy imports happen after freezing has dispatched here."""

import os
import time
import traceback
from pathlib import Path


def execute(name, output_text, connection, stop):
    output = Path(output_text)
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    def emit(kind, data):
        connection.send({"job": name, "pid": os.getpid(), "event": kind, "data": data})

    emit("started", {"cwd": os.getcwd()})
    try:
        if name.startswith("core-"):
            from openkb.portable_prototype.core_probe import run_core

            result = run_core(output, name[-1], emit)
        elif name == "renderers":
            from openkb.portable_prototype.render_probe import run_renderers

            result = run_renderers(output, emit)
        elif name == "transport":
            from openkb.portable_prototype.paths import assets

            os.environ["TIKTOKEN_CACHE_DIR"] = str(assets() / "tiktoken-cache")
            from openkb.portable_prototype.transport_probe import run_transport

            result = run_transport()
        elif name == "crash":
            from openkb.portable_prototype.core_probe import prepare_recovery

            prepare_recovery(output)
        elif name == "recover":
            from openkb.portable_prototype.core_probe import recover

            result = recover(output)
        elif name == "stop":
            from openkb.portable_prototype.core_probe import stop_at_boundary

            result = stop_at_boundary(output, stop, emit)
        else:
            raise ValueError(name)
        emit(
            "result",
            {"ok": True, "elapsed_ms": round((time.monotonic() - started) * 1000), **result},
        )
    except Exception:
        emit("result", {"ok": False, "traceback": traceback.format_exc()})
    finally:
        connection.close()
