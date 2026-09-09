"""Immutable original versions and shared bytes, independent of knowledge commits."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from openkb.inputs import PreparedInput
from openkb.locks import atomic_write_bytes, atomic_write_json, kb_ingest_lock, kb_read_lock
from openkb.mutation import _copy_file_atomic, mutation_scope
from openkb.state import HashRegistry

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE = re.compile(r"[0-9a-f]{32}\Z")


def content_id(value: Any) -> str:
    """Hash a canonical JSON value; timestamps and credentials never belong here."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def valid_id(value: Any, *, source: bool = False) -> str:
    if not isinstance(value, str) or not (_SOURCE if source else _DIGEST).fullmatch(value):
        raise ValueError("Invalid source-store identity")
    return value


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Invalid source-store record")
    return value


def normalized_origin(path: Path, kb_dir: Path, origin: str | None = None) -> str:
    if origin is not None:
        parts = urlsplit(origin)
        if parts.scheme in {"http", "https"} and parts.hostname:
            if parts.username is not None or parts.password is not None:
                raise ValueError("Source URLs must not include credentials")
            return urlunsplit(
                (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, "")
            )
        if origin.startswith("upload:") and len(origin) > len("upload:"):
            return origin
        raise ValueError("Invalid source origin")
    # PreparedInput already resolved this identity with the captured bytes.
    # Resolving again here could bind those bytes to a replacement symlink.
    path = path.absolute()
    if path.is_relative_to(kb_dir.resolve()):
        return "kb:" + os.path.normcase(path.relative_to(kb_dir.resolve()).as_posix())
    return "file:" + os.path.normcase(path.as_posix())


@dataclass(frozen=True)
class SourceVersion:
    id: str
    source_id: str
    origin: str
    name: str
    suffix: str
    blob: str
    assets: dict[str, str | None]
    input_key: str
    revision: int

    def __post_init__(self) -> None:
        valid_id(self.id)
        valid_id(self.source_id, source=True)
        valid_id(self.blob)
        valid_id(self.input_key)
        if not all(
            isinstance(item, str) and item for item in (self.origin, self.name, self.suffix)
        ):
            raise ValueError("Invalid source version details")
        if not isinstance(self.assets, dict) or not all(
            isinstance(key, str) for key in self.assets
        ):
            raise ValueError("Invalid source asset manifest")
        for asset in self.assets.values():
            if asset is not None:
                valid_id(asset)
        if self.input_key != content_id(
            {"blob": self.blob, "suffix": self.suffix, "assets": self.assets}
        ):
            raise ValueError("Source input manifest digest mismatch")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("Invalid source revision")
        if self.id != content_id(
            {
                "source": self.source_id,
                "input": self.input_key,
                "revision": self.revision,
                "origin": self.origin,
                "name": self.name,
            }
        ):
            raise ValueError("Source version digest mismatch")


class SourceStore:
    """Own provenance, immutable bytes and version checks behind a small interface.

    Immutable blobs can be left unreferenced by a crash; they are never exposed
    as completed intake until the source/version transaction commits. Knowledge
    transactions must not include this store in their rollback set.
    """

    def __init__(self, kb_dir: Path):
        self.kb_dir = kb_dir.resolve()
        self.root = self.kb_dir / ".openkb/source-store"
        self.owned_path(self.root)
        self.index = self.root / "sources.json"

    def owned_path(self, path: Path) -> Path:
        """Reject redirected state before either a read or a new blob write."""
        relative = path.relative_to(self.kb_dir)
        current = self.kb_dir
        for part in relative.parts:
            current /= part
            if part == ".." or current.is_symlink():
                raise ValueError("Source storage cannot contain redirected paths")
        return path

    def _records(self) -> dict[str, dict[str, str]]:
        if not self.index.exists():
            return {}
        records = read_object(self.owned_path(self.index))
        for origin, record in records.items():
            if not isinstance(record, dict) or set(record) != {"source_id", "current"}:
                raise ValueError("Invalid source identity registry")
            valid_id(record["source_id"], source=True)
            valid_id(record["current"])
            if not origin:
                raise ValueError("Invalid source origin")
        return records

    def _blob_path(self, digest: str) -> Path:
        return self.owned_path(self.root / "blobs" / valid_id(digest)[:2] / digest)

    def asset(self, digest: str | None) -> Path:
        if digest is None:
            raise ValueError("Required source asset is missing")
        path = self._blob_path(digest)
        if path.is_symlink() or HashRegistry.hash_file(path) != digest:
            raise ValueError("Immutable blob digest mismatch")
        return path

    def put_file(self, file: Path, digest: str | None = None) -> str:
        digest = digest or HashRegistry.hash_file(file)
        destination = self._blob_path(digest)
        with kb_ingest_lock(self.kb_dir / ".openkb"):
            if destination.exists():
                if HashRegistry.hash_file(file) != digest:
                    raise ValueError("Input changed before immutable publication")
                self.asset(digest)
            else:
                _copy_file_atomic(file, destination, expected_digest=digest)
                self.asset(digest)
        return digest

    def put_bytes(self, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        with kb_ingest_lock(self.kb_dir / ".openkb"):
            if self._blob_path(digest).exists():
                self.asset(digest)
            else:
                atomic_write_bytes(self._blob_path(digest), content)
        return digest

    def intake(self, ready: PreparedInput, *, origin: str | None = None) -> SourceVersion:
        """Commit the original and sidecar manifest before parsing/model work."""
        with kb_ingest_lock(self.kb_dir / ".openkb"):
            identity = normalized_origin(ready.identity, self.kb_dir, origin)
            records = self._records()
            previous = records.get(identity)
            source_id = previous["source_id"] if previous else uuid.uuid4().hex
            blob = self.put_file(ready.path, ready.digest)
            assets = {
                reference: self.put_file(image.path, image.digest) if image.path else None
                for reference, image in ready.images.items()
            }
            suffix = ready.source.suffix.lower()
            input_key = content_id({"blob": blob, "suffix": suffix, "assets": assets})
            current = self.version(previous["current"]) if previous else None
            if current is not None and current.input_key == input_key:
                return current
            revision = current.revision + 1 if current else 1
            version = SourceVersion(
                content_id(
                    {
                        "source": source_id,
                        "input": input_key,
                        "revision": revision,
                        "origin": identity,
                        "name": ready.source.name,
                    }
                ),
                source_id,
                identity,
                ready.source.name,
                suffix,
                blob,
                assets,
                input_key,
                revision,
            )
            path = self.owned_path(self.root / "versions" / f"{version.id}.json")
            if path.exists():
                saved = self.version(version.id)
                if saved != version:
                    raise ValueError("Immutable source version changed")
            records[identity] = {"source_id": source_id, "current": version.id}
            with mutation_scope(self.kb_dir, [path, self.index], operation="source intake"):
                if not path.exists():
                    atomic_write_json(path, asdict(version))
                atomic_write_json(self.index, records)
            return version

    def version(self, version_id: str) -> SourceVersion:
        with kb_read_lock(self.kb_dir / ".openkb"):
            result = SourceVersion(
                **read_object(
                    self.owned_path(self.root / "versions" / f"{valid_id(version_id)}.json")
                )
            )
            if result.id != version_id:
                raise ValueError("Source version identity mismatch")
            return result

    def original(self, version: SourceVersion) -> Path:
        if self.version(version.id) != version:
            raise ValueError("Source version does not belong to this store")
        return self.asset(version.blob)

    def current(self, source_id: str) -> SourceVersion:
        valid_id(source_id, source=True)
        with kb_read_lock(self.kb_dir / ".openkb"):
            matches = [
                row["current"] for row in self._records().values() if row["source_id"] == source_id
            ]
            if len(matches) != 1:
                raise ValueError("Source identity is missing or ambiguous")
            return self.version(matches[0])

    def list_sources(self) -> tuple[SourceVersion, ...]:
        with kb_read_lock(self.kb_dir / ".openkb"):
            return tuple(self.version(row["current"]) for row in self._records().values())
