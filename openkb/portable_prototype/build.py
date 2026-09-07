"""Build a disposable onedir bundle using an already provisioned, pinned environment."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]


def main():
    build = ROOT / ".build"
    wheels = build / "wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    # Keep build caches and prototype runtimes out of the input wheel. Production
    # files are copied byte-for-byte; the existing packaging rules still include skills.
    with tempfile.TemporaryDirectory(prefix="portable-wheel-") as staging:
        stage = Path(staging)
        shutil.copytree(
            REPO / "openkb",
            stage / "openkb",
            ignore=shutil.ignore_patterns(
                ".venv",
                ".runtime",
                ".build",
                "dist",
                "artifacts",
                "node_modules",
                "rust-helper",
                "evidence",
                "__pycache__",
                "web",
            ),
        )
        shutil.copytree(REPO / "skills", stage / "skills")
        for name in ("pyproject.toml", "README.md", "LICENSE"):
            shutil.copy2(REPO / name, stage / name)
        build_env = dict(os.environ, SETUPTOOLS_SCM_PRETEND_VERSION="0.0.0+portable.prototype")
        subprocess.run(
            [sys.executable, "-m", "hatchling", "build", "-t", "wheel", "-d", str(wheels)],
            cwd=stage,
            env=build_env,
            check=True,
        )
    wheel = max(wheels.glob("*.whl"), key=lambda p: p.stat().st_mtime)
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "--no-deps",
            "--reinstall",
            str(wheel),
        ],
        cwd=build,
        check=True,
    )
    env = dict(os.environ, LITELLM_LOCAL_MODEL_COST_MAP="True", OTEL_SDK_DISABLED="true")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(ROOT / "dist"),
            "--workpath",
            str(build / "freeze"),
            str(ROOT / "portable.spec"),
        ],
        cwd=build,
        env=env,
        check=True,
    )


if __name__ == "__main__":
    main()
