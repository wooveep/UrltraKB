"""Archive the onedir probe and record file hashes, links, modes and wheel metadata."""

import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    bundle = ROOT / "dist" / "OpenKBProbe"
    shutil.copy2(ROOT / "BUNDLE_README.txt", bundle / "README.txt")
    records = []
    for path in sorted(bundle.rglob("*")):
        row = {"path": str(path.relative_to(bundle)), "mode": oct(path.lstat().st_mode & 0o777)}
        if path.is_symlink():
            row["link"] = os.readlink(path)
        elif path.is_file():
            row.update(
                bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest()
            )
        else:
            continue
        records.append(row)
    evidence = ROOT / "evidence"
    evidence.mkdir(exist_ok=True)
    (evidence / "bundle-files.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    metadata = sorted(
        {
            d.metadata["Name"]: d.version
            for d in importlib.metadata.distributions(path=[str(bundle / "_internal")])
        }.items()
    )
    (evidence / "bundle-distributions.json").write_text(
        json.dumps(dict(metadata), indent=2), encoding="utf-8"
    )
    artifact = ROOT / "artifacts" / f"OpenKBProbe-{platform.system()}-x86_64"
    artifact.parent.mkdir(exist_ok=True)
    if os.name == "nt":
        archive = Path(shutil.make_archive(str(artifact), "zip", bundle.parent, bundle.name))
    else:
        archive = artifact.with_suffix(".tar.gz")
        with tarfile.open(archive, "w:gz", compresslevel=6, dereference=False) as stream:
            stream.add(bundle, arcname=bundle.name)
    facts = {
        "archive": archive.name,
        "bytes": archive.stat().st_size,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "file_count": len(records),
        "symlinks": sum("link" in r for r in records),
        "uncompressed_regular_bytes": sum(r.get("bytes", 0) for r in records),
        "python": platform.python_version(),
    }
    (evidence / "archive.json").write_text(json.dumps(facts, indent=2), encoding="utf-8")
    print(json.dumps(facts, indent=2))


if __name__ == "__main__":
    main()
