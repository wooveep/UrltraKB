"""Acceptance probe, not semantic approval. Run inside an isolated network namespace on Linux."""

import hashlib
import importlib.metadata
import json
import os
import platform
import socket
import subprocess
import time
from pathlib import Path

from bootstrap import ROOT, node_path
from render import FONTS, render_sample
from samples import SAMPLES


def main():
    started = time.monotonic()
    facts = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "os_release": platform.freedesktop_os_release() if os.name != "nt" else {},
        "libc": platform.libc_ver(),
        "display": os.environ.get("DISPLAY"),
        "session_type": os.environ.get("XDG_SESSION_TYPE"),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP"),
        "network_interfaces": socket.if_nameindex(),
        "node": subprocess.check_output([str(node_path()), "--version"], text=True).strip(),
        "python_packages": {
            d.metadata["Name"]: d.version for d in importlib.metadata.distributions()
        },
        "fonts": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in FONTS},
        "human_visual_verdict": "pending",
        "windows_11_evidence": "not obtained",
    }
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=2):
            facts["external_socket"] = "connected"
    except OSError as exc:
        facts["external_socket"] = str(exc)
    results = []
    for scale in (1, 1.5, 2, 4):
        batch = []
        for dark in (False, True):
            for sample in SAMPLES:
                result = render_sample(sample, dark, scale)
                stored = Path(result["directory"]) / "source.md"
                result["original_source_preserved"] = (
                    stored.read_text(encoding="utf-8") == sample["markdown"]
                )
                batch.append(result)
                results.append(result)
        (ROOT / "artifacts" / f"batch-both-{scale:g}.json").write_text(
            json.dumps(batch, ensure_ascii=False, indent=2)
        )
        print(
            f"scale={scale:g}: {sum(r['technical_expectation_met'] for r in batch)}/{len(batch)}"
            " technical expectations; visual verdict separate",
            flush=True,
        )
    facts["attempts"] = len(results)
    facts["expected_successes"] = sum(r["ok"] for r in results)
    facts["expected_errors"] = sum(r["expected_error"] and not r["ok"] for r in results)
    facts["technical_failures"] = [r for r in results if not r["technical_expectation_met"]]
    facts["source_preserved"] = all(r["original_source_preserved"] for r in results)
    facts["elapsed_seconds"] = round(time.monotonic() - started, 2)
    facts["runs"] = [
        {
            k: r[k]
            for k in (
                "id",
                "dark",
                "scale",
                "ok",
                "expected_error",
                "technical_expectation_met",
                "elapsed_ms",
                "original_source_preserved",
            )
        }
        for r in results
    ]
    target = ROOT / "evidence" / "runtime-facts.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(facts, ensure_ascii=False, indent=2) + "\n")
    print(target, flush=True)
    if facts["technical_failures"] or not facts["source_preserved"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
