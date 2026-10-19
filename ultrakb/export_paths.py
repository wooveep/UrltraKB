"""External exports cannot overwrite any knowledge base, including a damaged one."""

from pathlib import Path

from ultrakb.config import _is_kb_dir


def require_external_export(kb_dir: Path, destination: Path) -> Path:
    root, target = kb_dir.resolve(), destination.resolve()
    markers = (
        "kb.sqlite",
        "config.yaml",
        "hashes.json",
        "needs-repair.json",
        "initializing.json",
        "journal",
    )
    if target.is_relative_to(root) or any(
        _is_kb_dir(parent) or any((parent / ".ultrakb" / marker).exists() for marker in markers)
        for parent in (target, *target.parents)
    ):
        raise ValueError("Choose an export destination outside every knowledge base")
    return target
