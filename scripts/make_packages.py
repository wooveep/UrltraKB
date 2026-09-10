"""Run the existing packaging stages against one verified committed source export."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

from export_desktop_source import export_source, verify_source

ROOT = Path(__file__).resolve().parents[1]
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("package", "desktop", "verify", "release"))
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--build-dir", type=Path, default=ROOT / "build/packages")
    parser.add_argument("--dist-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--materials", default="")
    args = parser.parse_args()
    if args.stage == "release" and not args.materials:
        parser.error("release requires MATERIALS pointing to matching assembled distribution files")
    if args.stage != "package" and (
        platform.system() not in {"Linux", "Windows"}
        or platform.machine().lower() not in {"x86_64", "amd64"}
    ):
        parser.error("desktop builds require a native Linux or Windows x86_64 host")
    commit = subprocess.check_output(
        [
            "git",
            "--no-replace-objects",
            "-C",
            str(ROOT),
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{args.commit}^{{commit}}",
        ],
        text=True,
    ).strip()
    workspace = args.build_dir.resolve() / commit[:12] / platform.system().lower()
    source = workspace / "source"
    if source.exists():
        identity = verify_source(source)
        if identity["commit"] != commit:
            raise ValueError("Existing build directory belongs to another commit")
    else:
        identity = export_source(ROOT, source, commit)
    environment = dict(os.environ, SETUPTOOLS_SCM_PRETEND_VERSION=identity["version"])
    # Keep uv from accidentally installing this export into a caller's project environment.
    environment["UV_PROJECT_ENVIRONMENT"] = str(source / ".venv")
    suffix = ".exe" if os.name == "nt" else ""
    python = source / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    program = source / "packaging/desktop/dist/UrltraKB"
    inventory = workspace / "inventory.json"
    destination = args.dist_dir.resolve() / commit[:12]

    def run(*command):
        logger.info("Running %s", " ".join(map(str, command)))
        subprocess.run(list(map(str, command)), cwd=source, env=environment, check=True)

    if args.stage == "package":
        destination = destination / "python"
        run(args.uv, "build", "--python", "3.12.13", "--out-dir", destination)
        # This fork pins a local PageIndex wheel; include it for pip installations.
        for wheel in (source / "packaging/pageindex").glob("*+openkb.*.whl"):
            shutil.copy2(wheel, destination / wheel.name)
        checksums(destination)
        logger.info("Python packages: %s", destination)
    elif args.stage == "desktop":
        run(
            args.uv,
            "sync",
            "--frozen",
            "--python",
            "3.12.13",
            "--extra",
            "desktop",
            "--extra",
            "api",
            "--extra",
            "dev",
        )
        run(
            args.uv,
            "pip",
            "install",
            "--python",
            python,
            "-r",
            "packaging/desktop/build-requirements.txt",
        )
        run(
            python,
            "scripts/prepare_desktop_assets.py",
            "--cache-dir",
            args.build_dir.resolve() / "downloads",
        )
        run(python, "scripts/build_desktop.py")
        with tempfile.TemporaryDirectory(prefix="inventory-", dir=workspace) as temporary:
            fresh = Path(temporary) / "inventory.json"
            run(python, "scripts/inventory_desktop.py", "--source", source, "--output", fresh)
            fresh.replace(inventory)
        logger.info("Portable program: %s", program)
        logger.info("Verified inventory: %s", inventory)
    elif args.stage == "verify":
        if not inventory.is_file():
            parser.error("run make desktop for this COMMIT before make verify")
        evidence = Path(tempfile.mkdtemp(prefix="acceptance-", dir=workspace)) / "result"
        run(program / ("UrltraKBVerify" + suffix), "--output", evidence)
        logger.info("Acceptance evidence: %s", evidence)
    else:
        if not inventory.is_file():
            parser.error("run make desktop for this COMMIT before make release")
        materials = Path(args.materials).resolve()
        companion = destination / f"UrltraKB-{identity['version']}-materials.zip"
        run(
            python,
            "scripts/package_desktop.py",
            "materials",
            "--source",
            source,
            "--materials",
            materials,
            "--output",
            destination,
        )
        run(
            python,
            "scripts/package_desktop.py",
            "runtime",
            "--source",
            source,
            "--program",
            program,
            "--inventory",
            inventory,
            "--materials",
            materials,
            "--source-archive",
            companion,
            "--output",
            destination,
        )
        checksums(destination)
        logger.info("Delivery archives: %s", destination)
    if verify_source(source) != identity:
        raise ValueError("Source identity changed during packaging")


def checksums(directory: Path) -> None:
    rows = []
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.name != "SHA256SUMS.txt":
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            rows.append(f"{digest}  {path.name}\n")
    (directory / "SHA256SUMS.txt").write_text("".join(rows), encoding="utf-8")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
