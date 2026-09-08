"""Export one committed source tree and bind it to a portable development build."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

_TREES = {"openkb", "skills", "tests", "scripts", "packaging", "examples"}
_FILES = {
    ".gitignore",
    "README.md",
    "LICENSE",
    "pyproject.toml",
    "uv.lock",
    "docs/desktop.md",
    "docs/desktop-workbench.md",
    "docs/desktop-evidence/workbench/overview.png",
    "docs/desktop-evidence/workbench/documents.png",
    "docs/desktop-evidence/workbench/knowledge.png",
    "docs/desktop-evidence/workbench/conversations.png",
    "docs/desktop-evidence/workbench/artifacts.png",
    "docs/desktop-evidence/workbench/tasks.png",
    "docs/desktop-evidence/workbench/settings.png",
    "docs/golden-principles.md",
}
_GENERATED = {
    "openkb/rendering/assets",
    "openkb/rendering/rust-helper/target",
    "packaging/desktop/build",
    "packaging/desktop/dist",
}


def _source(name: str) -> bool:
    if not isinstance(name, str) or not name:
        return False
    path = PurePosixPath(name)
    if (
        not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in name
        or ":" in name
        or "__pycache__" in path.parts
        or path.suffix == ".pyc"
        or any(name == generated or name.startswith(generated + "/") for generated in _GENERATED)
    ):
        return False
    return not name.startswith("openkb/web/") and (name in _FILES or path.parts[0] in _TREES)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "--no-replace-objects", "-C", str(repo), *args], text=True, encoding="utf-8"
    ).strip()


def _blobs(repo: Path, sha: str) -> list[tuple[str, str, int]]:
    raw = subprocess.check_output(
        ["git", "--no-replace-objects", "-C", str(repo), "ls-tree", "-rz", sha]
    )
    result = []
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        path = name.decode("utf-8")
        if not _source(path):
            continue
        mode, kind, oid = metadata.decode("ascii").split()
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise ValueError(f"Source entry must be a regular file: {path}")
        result.append((path, oid, int(mode, 8) & 0o777))
    return result


def _source_files(root: Path):
    for directory, dirs, files in os.walk(root, followlinks=False):
        relative = Path(directory).relative_to(root)
        dirs[:] = [
            name
            for name in dirs
            if name != "__pycache__"
            and (relative / name).as_posix() not in _GENERATED
            and (relative.parts or name in _TREES or name == "docs")
        ]
        for name in dirs:
            if (Path(directory) / name).is_symlink():
                raise ValueError("Source directories must not be redirected")
        for name in files:
            path = relative / name
            if _source(path.as_posix()) and path.suffix != ".pyc":
                yield path.as_posix()


def export_source(repo: Path, output: Path, commit: str = "HEAD") -> dict[str, str]:
    """Use Git objects, never dirty files; leave an existing destination untouched."""
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    sha = _git(repo, "rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        raise ValueError("Invalid source commit")
    count = int(_git(repo, "rev-list", "--count", sha))
    identity = {"version": f"0.1.dev{count}+g{sha[:12]}", "commit": sha}
    blobs = _blobs(repo, sha)
    if not blobs:
        raise ValueError("No application source at the selected commit")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="openkb-source-", dir=output.parent) as temporary:
        staging = Path(temporary) / "source"
        staging.mkdir()
        # Raw objects bypass export-ignore/subst and checkout filters, including
        # untracked .git/info/attributes. Every selected blob is copied exactly.
        for name, oid, mode in blobs:
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as target:
                subprocess.run(
                    ["git", "--no-replace-objects", "-C", str(repo), "cat-file", "blob", oid],
                    stdout=target,
                    check=True,
                )
            path.chmod(mode)
        (staging / "openkb").mkdir(exist_ok=True)
        (staging / "openkb/_build_info.json").write_text(
            json.dumps(identity, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        record = {
            "schema": 1,
            **identity,
            "tree": _git(repo, "rev-parse", f"{sha}^{{tree}}"),
            "files": {name: _digest(staging / name) for name in sorted(_source_files(staging))},
        }
        (staging / "source-export.json").write_text(
            json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        staging.rename(output)
    return identity


def verify_source(root: Path) -> dict[str, str]:
    """Fail before freezing if source drifted after the committed export."""
    root = root.resolve()
    record = json.loads((root / "source-export.json").read_text(encoding="utf-8"))
    if (
        not isinstance(record, dict)
        or set(record) != {"schema", "version", "commit", "tree", "files"}
        or type(record["schema"]) is not int
        or record["schema"] != 1
        or not isinstance(record["files"], dict)
        or not record["files"]
        or not isinstance(record["commit"], str)
        or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", record["commit"])
        or not isinstance(record["tree"], str)
        or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", record["tree"])
        or not isinstance(record["version"], str)
        or not re.fullmatch(r"0\.1\.dev[0-9]+\+g[0-9a-f]{12}", record["version"])
    ):
        raise ValueError("Invalid committed source inventory")
    for name, digest in record["files"].items():
        if (
            not _source(name)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ValueError("Invalid source file record")
        path = root / name
        if (
            path.is_symlink()
            or path.resolve() != path
            or not path.is_file()
            or _digest(path) != digest
        ):
            raise ValueError(f"Exported source changed: {name}")
    if set(_source_files(root)) != set(record["files"]):
        raise ValueError("Unexpected application files in committed source export")
    identity = {"version": record["version"], "commit": record["commit"]}
    if json.loads((root / "openkb/_build_info.json").read_text("utf-8")) != identity:
        raise ValueError("Installed identity does not match committed source")
    return identity


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commit", default="HEAD")
    options = parser.parse_args()
    print(
        json.dumps(
            export_source(Path(__file__).resolve().parents[1], options.output, options.commit),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
