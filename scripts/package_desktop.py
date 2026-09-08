"""Create UrltraKB runtime archives separately from matching source/build materials."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import tarfile
import tempfile
import zipfile
from dataclasses import asdict
from pathlib import Path, PurePosixPath

from openkb.distribution import ReleaseFile, read_distribution

try:
    from .export_desktop_source import verify_source
except ImportError:
    from export_desktop_source import verify_source


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def record(path, kind):
    return ReleaseFile(path.name, kind, path.stat().st_size, digest(path))


def _target(output, name):
    output.mkdir(parents=True, exist_ok=True)
    target = output / name
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    return target


def _zip_members(path, expected):
    with zipfile.ZipFile(path) as archive:
        if len(archive.namelist()) != len(expected) or set(archive.namelist()) != set(expected):
            raise ValueError("Archive members do not match the selected inputs")
        for name, row in expected.items():
            with archive.open(name) as stream:
                if (
                    archive.getinfo(name).file_size != row.size
                    or hashlib.file_digest(stream, "sha256").hexdigest() != row.sha256
                ):
                    raise ValueError(f"Archived content changed: {name}")


def _zip_write(archive, name, reader, compression=zipfile.ZIP_STORED):
    info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    info.external_attr = 0o100644 << 16
    info.compress_type = compression
    with archive.open(info, "w", force_zip64=True) as writer:
        shutil.copyfileobj(reader, writer, length=1024 * 1024)


def _tar_metadata(info):
    info.uid = info.gid = info.mtime = 0
    info.uname = info.gname = ""
    info.pax_headers = {}
    info.mode = 0o755 if info.isdir() or info.mode & 0o111 else 0o644
    return info


def _material_members(materials, identity):
    release = read_distribution(materials, identity)
    if release.source_archive:
        raise ValueError("A complete material directory is required")
    members = {"UrltraKB/distribution/" + f.name: f for f in release.files}
    members["UrltraKB/distribution/release.json"] = record(materials / "release.json", "build")
    return release, members


def package_materials(source: Path, materials: Path, output: Path) -> ReleaseFile:
    identity = verify_source(source)
    release, expected = _material_members(materials, identity)
    target = _target(output, f"UrltraKB-{identity['version']}-materials.zip")
    with tempfile.TemporaryDirectory(prefix="urltrakb-materials-", dir=output) as directory:
        archive_path = Path(directory) / target.name
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
            with (materials / "release.json").open("rb") as reader:
                _zip_write(archive, "UrltraKB/distribution/release.json", reader)
            for file in release.files:
                with release.open_file(file.name) as reader:
                    _zip_write(archive, "UrltraKB/distribution/" + file.name, reader)
        _zip_members(archive_path, expected)
        archive_path.rename(target)
    return record(target, "source")


def _program_copy(program, inventory, destination, identity):
    if {key: inventory[key] for key in ("version", "commit")} != identity:
        raise ValueError("Program inventory does not match the exported source")
    rows = inventory["files"]
    names = [row["path"] for row in rows]
    suffix = ".exe" if inventory["platform"]["system"] == "Windows" else ""
    required = {
        name + suffix for name in ("UrltraKB", "UrltraKBCLI", "UrltraKBAPI", "UrltraKBVerify")
    }
    if len(set(names)) != len(names) or not required <= set(names):
        raise ValueError("Missing or duplicate UrltraKB entry points")
    for row in rows:
        name = row["path"]
        relative = PurePosixPath(name)
        if (
            relative.is_absolute()
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or "\\" in name
            or ":" in name
            or (name not in required and not name.startswith("_internal/"))
        ):
            raise ValueError("Unsafe or unexpected program inventory path")
        original = program / name
        if not original.resolve().is_relative_to(program.resolve()):
            raise ValueError("Program input escapes its directory")
        if (
            not original.is_file()
            or original.stat().st_size != row["size"]
            or digest(original) != row["sha256"]
        ):
            raise ValueError(f"Program input changed: {name}")
        if not suffix and name in required and not original.stat().st_mode & 0o100:
            raise ValueError(f"Linux entry point is not executable: {name}")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if original.is_symlink():
            if original.readlink().as_posix() != row.get("link"):
                raise ValueError(f"Program link changed: {name}")
            target.symlink_to(original.readlink())
        else:
            shutil.copy2(original, target)
    for row in rows:
        copied = destination / row["path"]
        if (
            not copied.resolve().is_relative_to(destination.resolve())
            or copied.stat().st_size != row["size"]
            or digest(copied) != row["sha256"]
        ):
            raise ValueError(f"Copied program input changed: {row['path']}")
    if (
        json.loads((destination / "_internal/openkb/_build_info.json").read_text("utf-8"))
        != identity
    ):
        raise ValueError("Program identity does not match its inventory")


def package_runtime(source, program, inventory, materials, source_archive, output):
    identity = verify_source(source)
    release, material_members = _material_members(materials, identity)
    if source_archive.name != f"UrltraKB-{identity['version']}-materials.zip":
        raise ValueError("Separate material archive must identify this UrltraKB version")
    companion = record(source_archive, "source")
    _zip_members(source_archive, material_members)
    if digest(source_archive) != companion.sha256:
        raise ValueError("Separate material archive changed during verification")
    system = inventory["platform"]["system"]
    if system not in {"Linux", "Windows"} or inventory["platform"]["machine"].lower() not in {
        "x86_64",
        "amd64",
    }:
        raise ValueError("Only Windows and Debian x86_64 builds are supported")
    platform = "windows-x64.zip" if system == "Windows" else "debian13.6-x64.tar.gz"
    target = _target(output, f"UrltraKB-{identity['version']}-{platform}")
    with tempfile.TemporaryDirectory(prefix="urltrakb-runtime-", dir=output) as directory:
        stage = Path(directory) / "UrltraKB"
        _program_copy(program, inventory, stage, identity)
        distribution = stage / "distribution"
        distribution.mkdir()
        bundled = []
        notices = [
            "UrltraKB runtime package\n",
            f"Version: {identity['version']}\nCommit: {identity['commit']}\n",
            f"Separate source/build materials: {companion.name}\nSHA256: {companion.sha256}\n",
            "完整许可随程序提供。源码与构建资料在上述独立资料包中，可从同一交付目录取得。\n"
            "需要本地源码或通过 REST 提供完整资料时，将资料包解压到与程序包相同的位置，\n"
            "合并 UrltraKB/distribution/。也可用 OPENKB_DISTRIBUTION_DIR 指向解压后的资料目录。\n"
            "以下完整资料说明中的源码路径属于独立资料包，不表示源码已随运行包安装。\n"
            "Download the companion archive from the same delivery location. Extract it beside\n"
            "the program archive to merge UrltraKB/distribution/, or configure the extracted\n"
            "material directory through OPENKB_DISTRIBUTION_DIR. Source paths in the original\n"
            "material notice below refer to that companion archive.\n",
        ]
        for file in release.files:
            if file.kind not in {"licenses", "notice"}:
                continue
            with release.open_file(file.name) as stream:
                if file.kind == "notice":
                    notices.append(stream.read().decode("utf-8"))
                else:
                    with (distribution / file.name).open("xb") as writer:
                        shutil.copyfileobj(stream, writer)
                    bundled.append(file)
        notice = distribution / "UrltraKB-NOTICE.txt"
        with notice.open("x", encoding="utf-8") as stream:
            stream.write("\n".join(notices))
        bundled.append(record(notice, "notice"))
        manifest = {
            "schema": 2,
            **identity,
            "source_archive": asdict(companion),
            "files": [asdict(f) for f in bundled],
        }
        (distribution / "release.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        read_distribution(distribution, identity)
        (stage / "README.txt").write_text(
            f"UrltraKB {identity['version']}\n\n"
            "Windows：运行 UrltraKB.exe。Debian：运行 ./UrltraKB。\n"
            "UrltraKBCLI 提供命令行，UrltraKBAPI 提供 REST，UrltraKBVerify 用于验收。\n"
            "保留整个程序目录。许可与独立源码资料包信息见 distribution/，"
            "也可从右上角 ⋯ 菜单查看。\n",
            encoding="utf-8",
        )
        expected = {
            "UrltraKB/" + p.relative_to(stage).as_posix(): record(p, "build")
            for p in stage.rglob("*")
            if p.is_file()
        }
        archive_path = Path(directory) / target.name
        if system == "Windows":
            with zipfile.ZipFile(
                archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
            ) as archive:
                for name in sorted(expected):
                    path = Path(directory) / name
                    with path.open("rb") as reader:
                        _zip_write(
                            archive,
                            name,
                            reader,
                            zipfile.ZIP_STORED if path.suffix == ".zip" else zipfile.ZIP_DEFLATED,
                        )
            _zip_members(archive_path, expected)
        else:
            with (
                archive_path.open("wb") as raw,
                gzip.GzipFile(
                    filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=6
                ) as compressed,
                tarfile.open(fileobj=compressed, mode="w") as archive,
            ):
                archive.add(stage, arcname="UrltraKB", filter=_tar_metadata)
            with tarfile.open(archive_path, "r:gz") as archive:
                entries = [m for m in archive.getmembers() if not m.isdir()]
                if len(entries) != len(expected) or {m.name for m in entries} != set(expected):
                    raise ValueError("Unexpected runtime archive members")
                for name, row in expected.items():
                    with archive.extractfile(name) as stream:
                        if hashlib.file_digest(stream, "sha256").hexdigest() != row.sha256:
                            raise ValueError(f"Archived runtime changed: {name}")
        archive_path.rename(target)
    return record(target, "build")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("materials", "runtime"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--materials", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--program", type=Path)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--source-archive", type=Path)
    args = parser.parse_args()
    if args.mode == "materials":
        result = package_materials(args.source, args.materials, args.output)
    else:
        if not all((args.program, args.inventory, args.source_archive)):
            parser.error("runtime requires --program, --inventory and --source-archive")
        result = package_runtime(
            args.source,
            args.program,
            json.loads(args.inventory.read_text("utf-8")),
            args.materials,
            args.source_archive,
            args.output,
        )
    print(json.dumps(asdict(result), indent=2))


if __name__ == "__main__":
    main()
