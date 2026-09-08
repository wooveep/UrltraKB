"""Assemble reviewed, checksum-pinned distribution materials without publishing."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from export_desktop_source import verify_source

KINDS = {"source", "licenses", "notice", "components", "build"}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def relative_path(value: str) -> PurePosixPath:
    if not isinstance(value, str):
        raise ValueError("Material paths must be strings")
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or any(part in {".", ".."} for part in value.split("/"))
        or "\\" in value
        or ":" in value
        or "\0" in value
        or path.as_posix() != value
    ):
        raise ValueError("Material paths must be normalized relative paths")
    return path


def validate(plan: dict, root: Path) -> None:
    """Check the reviewed input whitelist before creating any release output."""
    if plan.get("schema") != 1 or plan.get("unresolved") != []:
        raise ValueError("Corresponding-source or license review is incomplete")
    identity = plan["identity"]
    if (
        set(identity) != {"version", "commit"}
        or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", identity["commit"])
        or not re.fullmatch(r"[A-Za-z0-9.+_-]+", identity["version"])
    ):
        raise ValueError("Invalid program identity")
    if verify_source(root / "program-source") != identity:
        raise ValueError("Materials do not match the exported program source")
    groups = plan["groups"]
    if not 5 <= len(groups) <= 255 or {g["kind"] for g in groups} != KINDS:
        raise ValueError("All five distribution material kinds are required")
    names = [group["name"] for group in groups]
    if (
        bool({"release.json", "materials-plan.json"} & set(names))
        or len(set(names)) != len(names)
        or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", name) for name in names)
    ):
        raise ValueError("Invalid or duplicate release filenames")
    for group in groups:
        members = group["members"]
        if not members or group["format"] not in {"zip", "copy"}:
            raise ValueError("An archive requires explicit input members")
        if group["format"] == "copy" and len(members) != 1:
            raise ValueError("Plain materials require exactly one input")
        destinations = [relative_path(member["path"]).as_posix() for member in members]
        if len({name.casefold() for name in destinations}) != len(destinations):
            raise ValueError("Duplicate archive paths")
        for member in members:
            source = root / relative_path(member["input"])
            if source.is_symlink() or source.resolve() != source or not source.is_file():
                raise ValueError(f"Input must be a local regular file: {member['input']}")
            if (
                type(member["size"]) is not int
                or source.stat().st_size != member["size"]
                or digest(source) != member["sha256"]
            ):
                raise ValueError(f"Material input changed: {member['input']}")


def assemble(plan: dict, root: Path, output: Path) -> dict:
    """Copy only reviewed files; publish the manifest after all archives verify."""
    root, output = root.resolve(), output.absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    validate(plan, root)
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"schema": 1, **plan["identity"], "files": []}
    with tempfile.TemporaryDirectory(prefix="openkb-materials-", dir=output.parent) as directory:
        staging = Path(directory) / "distribution"
        staging.mkdir()
        for group in plan["groups"]:
            target = staging / group["name"]
            if group["format"] == "copy":
                shutil.copyfile(root / group["members"][0]["input"], target)
                if digest(target) != group["members"][0]["sha256"]:
                    raise ValueError("Input changed while copying")
            else:
                with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    for member in sorted(group["members"], key=lambda item: item["path"]):
                        source = root / member["input"]
                        info = zipfile.ZipInfo(member["path"], date_time=(2026, 1, 1, 0, 0, 0))
                        info.external_attr = (0o100644 | (source.stat().st_mode & 0o111)) << 16
                        # Source archives are already compressed; keep them verbatim.
                        info.compress_type = (
                            zipfile.ZIP_STORED
                            if source.suffix
                            in {".gz", ".xz", ".zst", ".zip", ".crate", ".tgz", ".rpm"}
                            else zipfile.ZIP_DEFLATED
                        )
                        with (
                            source.open("rb") as reader,
                            archive.open(info, "w", force_zip64=True) as writer,
                        ):
                            shutil.copyfileobj(reader, writer, length=1024 * 1024)
                # Verify archived bytes independently of the live input paths.
                with zipfile.ZipFile(target) as archive:
                    for member in group["members"]:
                        with archive.open(member["path"]) as stream:
                            actual = hashlib.file_digest(stream, "sha256").hexdigest()
                        if actual != member["sha256"]:
                            raise ValueError(f"Archived input changed: {member['input']}")
            manifest["files"].append(
                {
                    "name": group["name"],
                    "kind": group["kind"],
                    "size": target.stat().st_size,
                    "sha256": digest(target),
                }
            )
        plan_file = staging / "materials-plan.json"
        plan_file.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        manifest["files"].append(
            {
                "name": plan_file.name,
                "kind": "build",
                "size": plan_file.stat().st_size,
                "sha256": digest(plan_file),
            }
        )
        (staging / "release.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        staging.rename(output)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    print(json.dumps(assemble(plan, args.inputs, args.output), indent=2))


if __name__ == "__main__":
    main()
