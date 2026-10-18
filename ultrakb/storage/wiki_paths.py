"""Portable logical paths shared by immutable manifests and editable projections."""

from pathlib import PurePosixPath


def wiki_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or PurePosixPath(value).is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or value.split("/", 1)[0] == ".ultrakb"
    ):
        raise ValueError("Wiki paths must be logical relative paths")
    return value


def validate_file_tree(paths) -> None:
    files = set(paths)
    for path in files:
        wiki_path(path)
        if any(parent.as_posix() in files for parent in PurePosixPath(path).parents):
            raise ValueError("A Wiki path cannot be both a file and a directory")
