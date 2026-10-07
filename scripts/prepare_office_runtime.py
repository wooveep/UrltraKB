"""Prepare the complete pinned Office sidecar before desktop inventory/packaging.

Run using the project's locked Python environment. No system installation is made.
The original source archive is verified and retained in companion materials by the
release assembler; runtime licenses remain in their original full distribution.
"""

import argparse
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from openkb.locks import atomic_write_json
from openkb.office.inventory import digest, inventory
from openkb.office.probe import probe
from openkb.office.records import OfficeArtifact, OfficeManifest

REPOSITORY = Path(__file__).resolve().parents[1]


def verify(path: Path, record: dict):
    if path.stat().st_size != record["bytes"] or digest(path) != record["sha256"]:
        raise ValueError(f"Pinned Office artifact does not match: {path.name}")


def prepare(archive: Path, source: Path, output: Path, launcher: Path | None = None):
    lock_path = REPOSITORY / "openkb/office/runtime-lock.json"
    lock = json.loads(lock_path.read_text())
    if sys.platform not in {"linux", "win32"}:
        raise ValueError("Only Linux and Windows Office builds are supported")
    verify(archive, lock[sys.platform])
    verify(source, lock["source"])
    if output.exists():
        raise ValueError(f"Output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="office-prepare-", dir=output.parent) as temporary:
        stage = Path(temporary)
        extracted = stage / "extracted"
        extracted.mkdir()
        if sys.platform == "linux":
            with tarfile.open(archive) as bundle:
                bundle.extractall(stage / "debs", filter="data")
            packages = sorted((stage / "debs").rglob("*.deb"))
            if not packages:
                raise ValueError("Official Office archive contains no Debian packages")
            for package in packages:
                subprocess.run(["dpkg-deb", "-x", str(package), str(extracted)], check=True)
            runtime = extracted / "opt/libreoffice26.2"
            python, soffice = "program/python", "program/soffice"
        else:
            subprocess.run(
                [
                    "msiexec.exe",
                    "/a",
                    str(archive.resolve()),
                    "/qn",
                    f"TARGETDIR={extracted.resolve()}",
                    "/L*v",
                    str(stage / "msi.log"),
                ],
                check=True,
                timeout=300,
            )
            candidates = list(extracted.rglob("program/soffice.exe"))
            if len(candidates) != 1:
                raise ValueError("Administrative MSI extraction did not produce one Office tree")
            runtime = candidates[0].parent.parent
            python, soffice = "program/python.exe", "program/soffice.exe"
            if launcher is None:
                raise ValueError(
                    "Windows Office requires the separately built owned-process launcher"
                )
        fonts_path = REPOSITORY / "assets/fonts/manifest.json"
        license_dir = runtime / "openkb-provenance/licenses"
        license_dir.mkdir(parents=True)
        shutil.copy2(lock_path, runtime / "openkb-provenance/runtime-lock.json")
        shutil.copy2(fonts_path, runtime / "openkb-provenance/application-fonts.json")
        font_dir = runtime / "share/fonts/truetype"
        font_dir.mkdir(parents=True, exist_ok=True)
        for font in json.loads(fonts_path.read_text()):
            original = fonts_path.parent / font["file"]
            if digest(original) != font["sha256"]:
                raise ValueError(f"Application font hash mismatch: {original.name}")
            shutil.copy2(original, font_dir / original.name)
            shutil.copy2(fonts_path.parent / font["license"], license_dir / font["license"])
        private_launcher = None
        if sys.platform == "win32" and launcher:
            private_launcher = "openkb-provenance/office-launcher.exe"
            shutil.copy2(launcher, runtime / private_launcher)
        actual = probe(runtime, python, soffice, private_launcher)
        files, links = inventory(runtime)
        manifest = OfficeManifest(
            build_id=lock["build_id"],
            platform=sys.platform,
            archive=OfficeArtifact(**lock[sys.platform]),
            source=OfficeArtifact(**lock["source"]),
            python_version=lock["python_version"],
            python=python,
            soffice=soffice,
            launcher=private_launcher,
            probe=actual,
            files=files,
            links=links,
            fonts={
                name: value
                for name, value in files.items()
                if name.lower().endswith((".otf", ".ttf", ".ttc"))
            },
            licenses={
                name: value
                for name, value in files.items()
                if any(
                    word in name.lower()
                    for word in ("license", "copying", "notice", "ofl", "copyright")
                )
            },
            application_fonts_manifest=digest(fonts_path),
        )
        atomic_write_json(runtime / "openkb-office.json", manifest.model_dump(mode="json"))
        runtime.rename(output)
        print(f"Prepared {manifest.version} / {manifest.build_id}: {len(files)} files at {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--launcher", type=Path)
    arguments = parser.parse_args()
    prepare(arguments.archive, arguments.source, arguments.output, arguments.launcher)
