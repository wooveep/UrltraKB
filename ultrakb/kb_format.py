"""Supported knowledge-base format; creation is explicit and never a migration."""

from pathlib import Path


def require_current_kb(root: Path) -> None:
    from ultrakb.storage.database import open_database

    with open_database(root, read_only=True):
        pass


def validate_existing_format(root: Path) -> None:
    # Low-level locks also guard creation/recovery and plain scratch directories.
    # An existing configured KB must declare its format before lock-file creation
    # or mutation recovery can alter any of its own files.
    if any(
        (root / ".ultrakb" / name).exists() for name in ("config.yaml", "format.json", "kb.sqlite")
    ):
        require_current_kb(root)
