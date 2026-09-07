"""Contained filesystem versions and actual change facts for KB operations."""

import hashlib
from pathlib import Path


def contained_paths(kb_dir: Path, paths: list[Path]) -> list[Path]:
    root = kb_dir.resolve()
    for path in paths:
        if not path.resolve().is_relative_to(root) or path.resolve() == root:
            raise ValueError("Operation path escapes the knowledge base")
    return paths


def file_versions(kb_dir: Path, roots: list[Path]) -> dict[str, str]:
    versions = {}
    for root in roots:
        for path in sorted(root.rglob("*")) if root.is_dir() else [root]:
            contained_paths(kb_dir, [path])
            if path.is_file():
                digest = hashlib.sha256()
                with path.open("rb") as file:
                    for chunk in iter(lambda: file.read(1024 * 1024), b""):
                        digest.update(chunk)
                versions[path.relative_to(kb_dir).as_posix()] = digest.hexdigest()
    return versions


def changed_files(kb_dir: Path, roots: list[Path], before: dict[str, str]) -> tuple[str, ...]:
    after = file_versions(kb_dir, roots)
    return tuple(
        f"{'deleted' if path not in after else 'updated'}: {path}"
        for path in sorted(before.keys() | after.keys())
        if before.get(path) != after.get(path)
    )
