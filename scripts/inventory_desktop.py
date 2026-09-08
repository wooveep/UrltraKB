"""Trace a frozen directory to its actual build inputs, including embedded Python.

Run with the Python environment that produced the selected exported build.
This records provenance; it does not declare source or licence completeness.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import zipfile
from importlib import metadata
from pathlib import Path

from export_desktop_source import verify_source


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


class Inputs:
    def __init__(self, source: Path, identity: dict) -> None:
        self.source = source
        self.assets = source / "openkb/rendering/assets"
        self.components: dict[str, dict] = {}
        self.owners: dict[Path, tuple[str, str]] = {}
        self.used: set[str] = set()
        self.openkb = self.add("openkb", identity["version"], commit=identity["commit"])
        self.python = self.add("runtime/CPython", platform.python_version())
        for dist in metadata.distributions():
            name, version = dist.metadata["Name"], dist.version
            key = (
                self.openkb
                if normalized(name) == "openkb"
                else self.add(
                    "python/" + normalized(name),
                    version,
                    declared_license=dist.metadata.get("License-Expression")
                    or dist.metadata.get("License", ""),
                    project_urls=dist.metadata.get_all("Project-URL", []),
                )
            )
            for item in dist.files or ():
                self.owners[Path(dist.locate_file(item)).resolve()] = (key, item.as_posix())
        self.npm = json.loads((source / "openkb/rendering/package-lock.json").read_text("utf-8"))[
            "packages"
        ]
        for name, item in self.npm.items():
            if name:
                self.add(
                    "npm/" + name.removeprefix("node_modules/"),
                    item["version"],
                    declared_license=item.get("license", ""),
                    source_url=item["resolved"],
                    integrity=item["integrity"],
                )
        self.node = self.add("runtime/Node", "24.20.0")
        self.font = self.add("font/NotoSansCJK", "Sans2.004")
        self.renderer = self.add(
            "build/native-renderer",
            identity["version"],
            source="openkb/rendering/rust-helper",
            lock_sha256=digest(source / "openkb/rendering/rust-helper/Cargo.lock"),
        )
        self.generated = self.add(
            "build/desktop-assets",
            identity["version"],
            source="scripts/prepare_desktop_assets.py",
            lock_sha256=digest(source / "openkb/rendering/package-lock.json"),
        )
        self.vocabulary = self.add(
            "resource/tiktoken-vocabularies",
            metadata.version("tiktoken"),
            source="tiktoken_ext/openai_public.py",
        )
        self.system: dict[Path, tuple[str, str]] = {}

    def add(self, name: str, version: str, **fields) -> str:
        key = name + "@" + version
        self.components[key] = {"name": name, "version": version, **fields}
        return key

    def debian(self, path: Path) -> tuple[str, str]:
        if path in self.system:
            return self.system[path]
        result = subprocess.run(
            ["dpkg-query", "-S", str(path)], capture_output=True, text=True, check=True
        ).stdout.strip()
        package = result.rsplit(": ", 1)[0]
        value = subprocess.check_output(
            ["dpkg-query", "-W", "-f=${Version}\t${source:Package}\t${source:Version}", package],
            text=True,
        ).split("\t")
        if len(value) != 3:
            raise ValueError(f"Unrecognized Debian package metadata: {package}")
        key = self.add(
            "debian/" + package, value[0], source_package=value[1], source_version=value[2]
        )
        self.system[path] = key, path.as_posix()
        return self.system[path]

    def owner(self, path: Path) -> tuple[str, str]:
        path = path.resolve(strict=True)
        if path.is_relative_to(self.assets):
            relative = path.relative_to(self.assets).as_posix()
            for name in sorted(self.npm, key=len, reverse=True):
                if name and relative.startswith(name + "/"):
                    item = self.npm[name]
                    return "npm/" + name.removeprefix("node_modules/") + "@" + item[
                        "version"
                    ], relative
            if relative in {"node", "node.exe", "NODE-LICENSE"}:
                return self.node, relative
            if relative in {
                "fonts/LICENSE",
                "fonts/NotoSansCJKsc-Regular.otf",
                "fonts/NotoSansCJKsc-Bold.otf",
            }:
                return self.font, relative
            if relative in {"renderer", "renderer.exe"}:
                return self.renderer, relative
            if relative in {
                "package.json",
                "package-lock.json",
                "mathjax_render.mjs",
                "process_guard.mjs",
            }:
                original = self.source / "openkb/rendering" / relative
                if digest(original) != digest(path):
                    raise ValueError(f"Copied rendering source changed: {relative}")
                return self.openkb, "openkb/rendering/" + relative
            if relative in {"manifest.json", "node_modules/.package-lock.json"}:
                return self.generated, "openkb/rendering/assets/" + relative
            for name, item in self.npm.items():
                for executable in item.get("bin", {}):
                    if relative in {
                        f"node_modules/.bin/{executable}{suffix}" for suffix in ("", ".cmd", ".ps1")
                    }:
                        return self.generated, "openkb/rendering/assets/" + relative
            raise ValueError(f"Unrecognized generated rendering asset: {relative}")
        if path in self.owners:
            return self.owners[path]
        if path.is_relative_to(self.source / "packaging/desktop/build/token-cache"):
            return self.vocabulary, path.name
        if path.is_relative_to(self.source / "packaging/desktop/build/freeze"):
            if path.name == "base_library.zip":
                return self.python, "base_library.zip"
            raise ValueError(f"Unmapped generated input: {path.name}")
        if path.is_relative_to(self.source):
            relative = path.relative_to(self.source).as_posix()
            if relative.startswith(("openkb/", "skills/", "packaging/desktop/")):
                return self.openkb, relative
        if path.is_relative_to(Path(sys.base_prefix).resolve()):
            return self.python, path.relative_to(Path(sys.base_prefix).resolve()).as_posix()
        if sys.platform == "linux" and path.is_relative_to(Path("/usr/lib")):
            return self.debian(path)
        raise ValueError(f"No component owns collected input: {path}")

    def record(self, source: str, *, namespace: str | None = None) -> dict:
        if source == "-" and namespace:
            return {"namespace": namespace}
        path = Path(source)
        component, relative = self.owner(path)
        self.used.add(component)
        return {"component": component, "input": relative, "input_sha256": digest(path)}


def inventory(source: Path, program: Path, analysis: Path) -> dict:
    import PyInstaller
    from PyInstaller.archive.readers import CArchiveReader

    source, program = source.resolve(), program.resolve()
    identity = verify_source(source)
    if metadata.version("openkb") != identity["version"]:
        raise ValueError("Use the environment that installed this exact source export")
    installed = json.loads((program / "_internal/openkb/_build_info.json").read_text("utf-8"))
    if installed != identity:
        raise ValueError("Frozen identity does not match exported source")
    toc = ast.literal_eval(analysis.read_text(encoding="utf-8"))
    if not isinstance(toc, tuple) or len(toc) != 20:
        raise ValueError("Unrecognized pinned PyInstaller analysis format")
    inputs = Inputs(source, identity)
    modules = {name: inputs.record(path, namespace=name) for name, path, _ in toc[14]}
    scripts = {name: inputs.record(path) for name, path, _ in toc[13]}
    collected = {}
    links = {}
    for name, path, kind in [*toc[15], *toc[18]]:
        name = Path(name).as_posix()
        if kind == "SYMLINK":
            links[name] = path
        else:
            collected[name] = inputs.record(path)
    for name, target in links.items():
        resolved = (program / "_internal" / name).resolve(strict=True)
        if not resolved.is_relative_to(program / "_internal"):
            raise ValueError(f"Collected link escapes program directory: {name}")
        destination = resolved.relative_to(program / "_internal").as_posix()
        if destination not in collected:
            raise ValueError(f"Collected link has no component mapping: {name}")
        collected[name] = {**collected[destination], "link": target}
    python_base = {name: inputs.record(path) for name, path, _ in toc[19]}
    expected_base = {
        name.replace(".", "/") + ("/__init__.pyc" if Path(path).name == "__init__.py" else ".pyc")
        for name, path, _ in toc[19]
    }
    with zipfile.ZipFile(program / "_internal/base_library.zip") as archive:
        if set(archive.namelist()) != expected_base or len(archive.namelist()) != len(
            expected_base
        ):
            raise ValueError("Base library differs from the build analysis")
    files = []
    expected_executables = {
        name + (".exe" if os.name == "nt" else "")
        for name in ("UrltraKB", "UrltraKBCLI", "UrltraKBAPI", "UrltraKBVerify")
    }
    found_executables = set()
    embedded_bootstrap = {}
    for path in sorted(program.rglob("*")):
        if path.is_dir():
            if path.is_symlink():
                raise ValueError("Program directory links require explicit inventory support")
            continue
        if not path.is_file():
            raise ValueError(f"Unexpected non-file in program: {path.name}")
        name = path.relative_to(program).as_posix()
        item = {"path": name, "size": path.stat().st_size, "sha256": digest(path)}
        if name in expected_executables:
            if path.is_symlink():
                raise ValueError(f"Product executable must not be a link: {name}")
            found_executables.add(name)
            built = analysis.parent / name
            if not built.is_file() or digest(built) != item["sha256"]:
                raise ValueError(f"Executable differs from the recorded build: {name}")
            archive = CArchiveReader(str(path))
            if any(
                entry[-1] not in {"s", "m"} and (module != "PYZ.pyz" or entry[-1] != "z")
                for module, entry in archive.toc.items()
            ):
                raise ValueError(f"Unmapped embedded executable payload: {name}")
            embedded = archive.open_embedded_archive("PYZ.pyz")
            bootstrap_modules = {name for name, entry in archive.toc.items() if entry[-1] == "m"}
            if set(embedded.toc) != set(modules) - bootstrap_modules:
                raise ValueError(f"Embedded Python differs from analysis: {name}")
            if not set(scripts).issubset(archive.toc):
                raise ValueError(f"Bootstrap scripts differ from analysis: {name}")
            bootstrap = {}
            for module, entry in archive.toc.items():
                if entry[-1] not in {"s", "m"}:
                    continue
                if module in scripts:
                    bootstrap[module] = scripts[module]
                elif module in modules:
                    bootstrap[module] = modules[module]
                else:
                    loader = Path(PyInstaller.__file__).parent / "loader" / (module + ".py")
                    bootstrap[module] = inputs.record(str(loader))
            if embedded_bootstrap and bootstrap != embedded_bootstrap:
                raise ValueError("Entry points have different bootstrap contents")
            embedded_bootstrap = bootstrap
            item.update(
                container="PyInstaller executable",
                python_modules="python_modules",
                bootstrap="bootstrap",
                component="python/pyinstaller@" + metadata.version("pyinstaller"),
            )
            inputs.used.add(item["component"])
        elif name.startswith("_internal/") and name.removeprefix("_internal/") in collected:
            relative = name.removeprefix("_internal/")
            item.update(collected[relative])
            if path.is_symlink():
                if relative not in links:
                    raise ValueError(f"Unexpected link in program directory: {name}")
                target = (path.parent / links[relative]).resolve(strict=True)
                if not target.is_relative_to(program) or path.resolve(strict=True) != target:
                    raise ValueError(f"Program link differs from build input: {name}")
                item["link"] = os.readlink(path)
            if item["sha256"] != item["input_sha256"]:
                raise ValueError(f"Collected file differs from its build input: {name}")
        else:
            raise ValueError(f"File has no build input mapping: {name}")
        files.append(item)
    if found_executables != expected_executables:
        raise ValueError("One or more product entry points are missing")
    missing = {"_internal/" + name for name in collected} - {item["path"] for item in files}
    if missing:
        raise ValueError(f"Collected program files are missing: {sorted(missing)}")
    return {
        "schema": 1,
        **identity,
        "scope": (
            "Actual frozen files and primary build inputs; "
            "nested dependency/source/licence audit required"
        ),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "tools": {
            name: metadata.version(name) for name in ("pyinstaller", "pyinstaller-hooks-contrib")
        },
        "components": {key: inputs.components[key] for key in sorted(inputs.used)},
        "files": files,
        "python_modules": modules,
        "bootstrap": embedded_bootstrap,
        "base_library": python_base,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    packaging = args.source / "packaging/desktop"
    result = inventory(
        args.source, packaging / "dist/UrltraKB", packaging / "build/freeze/desktop/Analysis-00.toc"
    )
    with args.output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    print(
        f"Mapped {len(result['files'])} files and {len(result['python_modules'])} embedded modules"
    )


if __name__ == "__main__":
    main()
