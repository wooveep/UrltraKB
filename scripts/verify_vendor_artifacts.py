"""Verify built SDK wheels/sdists and the complete OpenKB source distribution."""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

from local_vendors import VENDORS, verify_vendor_source


def archive_files(path: Path) -> dict[str, bytes]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return {
                item.filename: archive.read(item)
                for item in archive.infolist()
                if not item.is_dir()
            }
    with tarfile.open(path) as archive:
        return {
            item.name.split("/", 1)[1]: archive.extractfile(item).read()
            for item in archive.getmembers()
            if item.isfile()
        }


def require_files(files: dict[str, bytes], expected: dict[str, bytes], name: str) -> None:
    changed = sorted(path for path, content in expected.items() if files.get(path) != content)
    if changed:
        raise ValueError(f"{name}: missing or changed files: {', '.join(changed)}")


def verify_artifacts(source: Path, dist: Path) -> dict:
    """Check actual archive bytes against the audited checkout, not build exit codes."""
    report = {}
    all_sources = {}
    for folder, distribution, module, version, _ in VENDORS:
        root = source / "vendor" / folder
        verify_vendor_source(root)
        provenance = json.loads((root / "UPSTREAM.json").read_text("utf-8"))
        retained = {
            name: (root / name).read_bytes()
            for name in [*provenance["included_files"], "UPSTREAM.json"]
        }
        all_sources.update(
            {f"vendor/{folder}/{name}": content for name, content in retained.items()}
        )
        runtime = {
            name: content for name, content in retained.items() if name.startswith(module + "/")
        }
        for suffix in ("-py3-none-any.whl", ".tar.gz"):
            path = dist / f"{distribution.replace('-', '_')}-{version}{suffix}"
            files = archive_files(path)
            if any(
                part in {".git", "__pycache__", "enterprise"}
                for name in files
                for part in Path(name).parts
            ):
                raise ValueError(f"Unexpected generated or excluded files in {path.name}")
            require_files(files, runtime, path.name)
            licenses = [data for name, data in files.items() if Path(name).name == "LICENSE"]
            if retained["LICENSE"] not in licenses:
                raise ValueError(f"Missing original license in {path.name}")
            if suffix == ".tar.gz":
                require_files(
                    files,
                    {
                        name: retained[name]
                        for name in (
                            "UPSTREAM.json",
                            "pyproject.toml",
                            "README.urltrakb.md",
                        )
                    },
                    path.name,
                )
            else:
                metadata_file = next(name for name in files if name.endswith(".dist-info/METADATA"))
                metadata = BytesParser().parsebytes(files[metadata_file])
                if metadata["Name"] != distribution or metadata["Version"] != version:
                    raise ValueError(f"Wrong distribution identity in {path.name}")
                if distribution == "litellm" and any(
                    name.endswith("entry_points.txt") for name in files
                ):
                    raise ValueError("LiteLLM must not install standalone service commands")
            report[path.name] = {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "runtime_files_checked": len(runtime),
                "original_license": True,
            }
    candidates = list(dist.glob("openkb-*.tar.gz"))
    if len(candidates) != 1:
        raise ValueError("Provide a dist directory with exactly one OpenKB source distribution")
    path = candidates[0]
    require_files(archive_files(path), all_sources, path.name)
    report[path.name] = {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "vendor_source_files_checked": len(all_sources),
    }
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify_artifacts(Path(__file__).resolve().parents[1], args.dist), indent=2))
