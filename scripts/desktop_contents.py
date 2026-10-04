"""Keep runtime assets focused and share identical Linux executable payloads."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath

ENTRYPOINTS = ("UrltraKB", "UrltraKBCLI", "UrltraKBAPI", "UrltraKBVerify")


def runtime_asset(relative: str) -> bool:
    """MathJax runs its bundled JS, not its duplicate module/development trees.

    Retain every dynamic bundle, font file and license. The NewCM package stays
    complete; this filter only trims the separate MathJax source package.
    """
    path = PurePosixPath(relative)
    prefix = PurePosixPath("node_modules/@mathjax/src")
    if path.is_relative_to(prefix):
        parts = path.relative_to(prefix).parts
        return bool(parts) and (
            parts[0] == "bundle"
            or (len(parts) == 1 and parts[0] in {"package.json", "LICENSE", "README.md"})
        )
    return True


def deduplicate_entrypoints(program: Path) -> int:
    """Linux bootloaders are identical; hard links retain each dispatch name.

    Windows uses different GUI/console bootloaders and is left untouched. Only
    byte-identical files with equal executable modes are shared. No application
    data or native libraries are rewritten.
    """
    if os.name != "posix":
        return 0
    seen: dict[tuple[str, int], Path] = {}
    saved = 0
    for name in ENTRYPOINTS:
        path = program / name
        stat = path.stat()
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        key = digest, stat.st_mode
        original = seen.get(key)
        if original is None:
            seen[key] = path
        elif not path.samefile(original):
            temporary = path.with_name(name + ".dedup")
            try:
                os.link(original, temporary)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            saved += stat.st_size
    return saved
