"""Add checksum-pinned Office source, notices, licenses and build records to a material plan.

The existing plan's unresolved reviews are preserved; this does not mark the
complete release's license audit or platform acceptance as passed.
"""

import argparse
import json
import shutil
import tempfile
from pathlib import Path

from openkb.office.inventory import digest
from openkb.office.runtime import validate_runtime


def add_office_materials(runtime: Path, source: Path, inputs: Path, plan: Path, output: Path):
    from prepare_office_runtime import verify

    manifest = validate_runtime(runtime)
    verify(source, manifest.source.model_dump())
    value = json.loads(plan.read_text("utf-8"))
    if any(group["name"].startswith("office-") for group in value["groups"]):
        raise ValueError("This material plan already includes Office")
    destination = inputs / "office-inputs"
    if destination.exists() or output.exists():
        raise FileExistsError("Office material output already exists")
    inputs.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="office-materials-", dir=inputs) as temporary:
        staging = Path(temporary)
        shutil.copy2(source, staging / source.name)
        for name in manifest.licenses:
            target = staging / "licenses" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(runtime / name, target)
        shutil.copy2(runtime / "openkb-office.json", staging / "office-manifest.json")
        (staging / "NOTICE.txt").write_text(
            f"LibreOffice {manifest.version}, build {manifest.build_id}\n"
            f"Official complete binary distribution: {manifest.archive.url}\n"
            f"SHA256: {manifest.archive.sha256}\n"
            f"Corresponding source: {manifest.source.url}\nSHA256: {manifest.source.sha256}\n"
            "Unmodified TDF binaries, bundled Python/UNO and bundled fonts.\n"
            "Application fonts are added unmodified under their retained OFL notices.\n"
            "See the complete upstream LICENSE for bundled third-party terms "
            "and source references.\n"
            "Build/staging rule: scripts/prepare_office_runtime.py "
            "in the committed application source.\n"
            "Windows launcher source: openkb/office/launcher (Rust 1.95.0, no external crates).\n",
            encoding="utf-8",
        )
        members = {kind: [] for kind in ("source", "licenses", "notice", "components")}
        for path in sorted(staging.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(staging).as_posix()
            kind = (
                "licenses"
                if relative.startswith("licenses/")
                else {
                    source.name: "source",
                    "NOTICE.txt": "notice",
                    "office-manifest.json": "components",
                }[relative]
            )
            members[kind].append(
                {
                    "input": "office-inputs/" + relative,
                    "path": "office-inputs/" + relative,
                    "size": path.stat().st_size,
                    "sha256": digest(path),
                }
            )
        for kind, rows in members.items():
            value["groups"].append(
                {
                    "name": f"office-{kind}.zip",
                    "kind": kind,
                    "format": "zip",
                    "members": rows,
                }
            )
        staging.rename(destination)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "source", "inputs", "plan", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    add_office_materials(args.runtime, args.source, args.inputs, args.plan, args.output)
