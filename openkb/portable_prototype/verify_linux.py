"""Launch the extracted bundle in a disposable, offline Debian runtime container."""

import argparse
import json
import os
import socket
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, default=ROOT / "dist" / "OpenKBProbe")
    parser.add_argument("--label", default="diagnostic")
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args()
    output = ROOT / "artifacts" / f"{args.label}-{int(time.time())}"
    output.mkdir(parents=True)
    authority = os.environ.get("XAUTHORITY", f"/run/user/{os.getuid()}/gdm/Xauthority")
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--hostname",
        socket.gethostname(),
        "--mount",
        f"type=bind,source={args.bundle.resolve()},target=/opt/便携 应用/OpenKBProbe,readonly",
        "--mount",
        f"type=bind,source={output},target=/data/测试结果",
        "--mount",
        "type=bind,source=/tmp/.X11-unix,target=/tmp/.X11-unix,readonly",
        "--mount",
        f"type=bind,source={authority},target=/tmp/probe-xauth,readonly",
        "openkb-portable-probe-debian:13.6",
        "/opt/便携 应用/OpenKBProbe/OpenKBProbe",
        "--output",
        "/data/测试结果",
        "--no-tray",
        "--use-system-profile",
    ]
    if not args.keep_open:
        command.append("--auto-exit")
    (output / "invocation.json").write_text(
        json.dumps(command, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(output, flush=True)
    with (output / "console.log").open("w", encoding="utf-8") as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
    print(f"Container exit {result.returncode}")
    summary = output / "summary.json"
    if summary.exists():
        facts = json.loads(summary.read_text(encoding="utf-8"))
        print(
            json.dumps(
                {
                    "all_expected": facts["all_expected"],
                    "results": {
                        key: value for key, value in facts["results"].items() if not value.get("ok")
                    },
                },
                indent=2,
            )
        )
    else:
        print((output / "console.log").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
