"""Hash the complete private distribution, including contained symbolic links."""

import hashlib
from pathlib import Path


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def inventory(root: Path) -> tuple[dict[str, str], dict[str, str]]:
    root = root.resolve()
    files, links = {}, {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if name == "openkb-office.json":
            continue
        if not path.resolve().is_relative_to(root):
            raise ValueError(f"Office runtime has an escaping link: {name}")
        if path.is_symlink():
            links[name] = path.resolve(strict=True).relative_to(root).as_posix()
        if path.is_file():
            files[name] = digest(path)
    return files, links
