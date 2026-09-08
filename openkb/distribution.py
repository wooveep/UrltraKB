"""Validated, fixed release materials shared by the desktop and REST product."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, BinaryIO

from openkb import __version__

_KINDS = {"source", "licenses", "notice", "components", "build"}


class DistributionError(ValueError):
    """Installed release materials are absent, inconsistent or damaged."""


def _open_regular(path: Path) -> BinaryIO:
    if path.is_symlink() or path.resolve() != path or not stat.S_ISREG(path.stat().st_mode):
        raise DistributionError("Release material must be a local regular file")
    # Nonblocking/no-follow close the POSIX FIFO/symlink race between checking
    # the directory entry and opening it. Verify the actual opened object too.
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    stream = os.fdopen(os.open(path, flags), "rb")
    try:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise DistributionError("Release material must be a regular file")
    except BaseException:
        stream.close()
        raise
    return stream


def _json(path: Path) -> Any:
    with _open_regular(path) as stream:
        content = stream.read(1024 * 1024 + 1)
    if len(content) > 1024 * 1024:
        raise DistributionError("Distribution manifest is too large")
    return json.loads(content.decode("utf-8"))


@dataclass(frozen=True)
class ReleaseFile:
    name: str
    kind: str
    size: int
    sha256: str

    @classmethod
    def parse(cls, value: Any) -> ReleaseFile:
        if not isinstance(value, dict) or set(value) != {"name", "kind", "size", "sha256"}:
            raise DistributionError("Invalid release file record")
        name, kind, size, digest = (value[k] for k in ("name", "kind", "size", "sha256"))
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,199}", name)
            or not isinstance(kind, str)
            or kind not in _KINDS
            or type(size) is not int
            or size < 0
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise DistributionError("Invalid release file identity")
        return cls(name, kind, size, digest)


@dataclass(frozen=True)
class Distribution:
    root: Path | None
    version: str
    commit: str | None
    files: tuple[ReleaseFile, ...]
    source_archive: ReleaseFile | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "status": "packaged" if self.root else "development",
            "version": self.version,
            "commit": self.commit,
            "source_license": "Apache-2.0 (original OpenKB source)",
            "distribution_terms": (
                "Combined distribution under AGPLv3; independent component terms retained."
                if self.root
                else "Development environment; matching distribution materials are not configured."
            ),
            "files": [asdict(file) for file in self.files],
            **({"source_archive": asdict(self.source_archive)} if self.source_archive else {}),
        }

    def open_file(self, name: str) -> BinaryIO:
        """Return a verified private snapshot; the caller must close the handle."""
        file = next((file for file in self.files if file.name == name), None)
        if file is None or self.root is None:
            raise KeyError(name)
        path = self.root / file.name
        try:
            snapshot = tempfile.TemporaryFile(mode="w+b")
        except OSError as exc:
            raise DistributionError("Release file snapshot could not be created") from exc
        try:
            digest = hashlib.sha256()
            with _open_regular(path) as stream:
                info = os.fstat(stream.fileno())
                if info.st_size != file.size:
                    raise DistributionError("Release file identity does not match its manifest")
                size = 0
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    size += len(chunk)
                    if size > file.size:
                        raise DistributionError("Release file changed during verification")
                    digest.update(chunk)
                    snapshot.write(chunk)
            if digest.hexdigest() != file.sha256:
                raise DistributionError("Release file checksum does not match its manifest")
            snapshot.seek(0)
            return snapshot
        except OSError as exc:
            snapshot.close()
            raise DistributionError("Release file could not be read") from exc
        except BaseException:
            snapshot.close()
            raise


def load_distribution() -> Distribution:
    """Never infer release provenance from a cwd, a KB, or the current Git branch."""
    configured = os.environ.get("OPENKB_DISTRIBUTION_DIR")
    frozen = bool(getattr(sys, "frozen", False))
    if not configured and not frozen:
        return Distribution(None, __version__, None, ())
    root = (
        Path(configured).expanduser().resolve()
        if configured
        else Path(sys.executable).resolve().parent / "distribution"
    )
    try:
        identity = _json(Path(__file__).with_name("_build_info.json"))
        if not isinstance(identity, dict) or identity.get("version") != __version__:
            raise DistributionError("Distribution version does not match this installation")
        return read_distribution(root, identity)
    except (OSError, ValueError, TypeError) as exc:
        raise DistributionError("Matching release materials are unavailable or invalid") from exc


def read_distribution(root: Path, identity: dict[str, str]) -> Distribution:
    """Read complete or split materials against a verified installation/export identity."""
    root = root.resolve()
    try:
        path = root / "release.json"
        if path.is_symlink():
            raise DistributionError("Release manifest must not be redirected")
        value = _json(path)
        fields = {"schema", "version", "commit", "files"}
        if not isinstance(value, dict):
            raise DistributionError("Invalid distribution manifest")
        split = value.get("schema") == 2
        if set(value) != (fields | {"source_archive"} if split else fields):
            raise DistributionError("Invalid distribution manifest")
        if (
            type(value["schema"]) is not int
            or value["schema"] not in {1, 2}
            or not isinstance(value["version"], str)
            or not re.fullmatch(r"[A-Za-z0-9.+_-]+", value["version"])
            or not isinstance(value["commit"], str)
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value["commit"])
            or not isinstance(value["files"], list)
            or not (2 if split else 5) <= len(value["files"]) <= 256
        ):
            raise DistributionError("Distribution version or identity is invalid")
        files = tuple(ReleaseFile.parse(file) for file in value["files"])
        kinds = {"licenses", "notice"} if split else _KINDS
        if len({file.name for file in files}) != len(files) or {f.kind for f in files} != kinds:
            raise DistributionError("Distribution materials are incomplete or duplicated")
        if identity != {"version": value["version"], "commit": value["commit"]}:
            raise DistributionError("Distribution materials do not match this installation")
        source_archive = ReleaseFile.parse(value["source_archive"]) if split else None
        if source_archive and (
            source_archive.kind != "source" or source_archive.name in {f.name for f in files}
        ):
            raise DistributionError("Invalid separate source archive")
        return Distribution(root, value["version"], value["commit"], files, source_archive)
    except (OSError, ValueError, TypeError) as exc:
        raise DistributionError("Matching release materials are unavailable or invalid") from exc
