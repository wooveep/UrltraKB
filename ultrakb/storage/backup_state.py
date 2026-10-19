"""Validated, independently coordinated files accompanying an online DB snapshot."""

import hashlib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ultrakb.configuration_lock import configuration_lock_path, require_prepared_configuration
from ultrakb.locks import file_write_lock

from .database import DatabaseIdentity
from .wiki_paths import validate_file_tree, wiki_path

CONFIGURATION_PATHS = (".ultrakb/config.yaml", ".env")


class BackupFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: Literal[".ultrakb/config.yaml", ".env"]
    role: Literal["configuration"]
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_count: int = Field(ge=0)


class BackupWikiChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_count: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def check_path(cls, value):
        return wiki_path(value)


class BackupWorktree(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    view_id: str = Field(min_length=1)
    edit_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    changes: tuple[BackupWikiChange, ...]
    removed: tuple[str, ...]

    @field_validator("removed")
    @classmethod
    def check_removed(cls, value):
        for path in value:
            wiki_path(path)
        return value


class BackupPendingReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(min_length=1)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_count: int = Field(ge=0)
    state: Literal["repairable", "unresolved"]

    @field_validator("name")
    @classmethod
    def check_name(cls, value):
        wiki_path(value)
        if "/" in value:
            raise ValueError("A pending receipt requires one filename")
        return value


class BackupManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    format: Literal["ultrakb.backup"] = "ultrakb.backup"
    version: Literal[2] = 2
    kb_id: str
    generation: str
    referenced_blobs: int = Field(ge=0)
    coverage: dict[str, Literal["complete", "absent", "partial", "not_captured", "external"]]
    files: tuple[BackupFile, ...]
    worktrees: tuple[BackupWorktree, ...] = ()
    pending_receipts: tuple[BackupPendingReceipt, ...] = ()

    @model_validator(mode="after")
    def validate_coverage(self) -> "BackupManifest":
        expected = {
            "committed_database": "complete",
            "referenced_blobs": "complete",
            "configuration": configuration_coverage(self.files),
            "inherited_configuration": "external",
            "worktree": "complete",
            "external_receipts": "partial"
            if any(packet.state == "unresolved" for packet in self.pending_receipts)
            else "complete",
        }
        if self.coverage != expected:
            raise ValueError("Backup coverage does not match its captured file manifest")
        return self


def configuration_coverage(files):
    if any(f.path == CONFIGURATION_PATHS[0] for f in files):
        return "complete"
    return "partial" if files else "absent"


def fingerprint(path: Path) -> tuple[str, int]:
    hasher, size = hashlib.sha256(), 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            hasher.update(chunk)
            size += len(chunk)
    return hasher.hexdigest(), size


def state_path(root: Path, relative: str) -> Path:
    path = root / relative
    for item in (path, *path.parents):
        if item == root:
            break
        if item.is_symlink():
            raise ValueError("Backup state cannot follow symbolic links")
    return path


def capture_configuration(root: Path, destination: Path, copy_verified):
    """Capture the local pair only; inherited global configuration remains external."""
    files = []
    with file_write_lock(configuration_lock_path(root)):
        require_prepared_configuration(root)
        for relative in CONFIGURATION_PATHS:
            source = state_path(root, relative)
            if not source.exists():
                continue
            digest, size = fingerprint(source)
            copy_verified(source, destination / relative, digest)
            files.append(
                BackupFile(path=relative, role="configuration", digest=digest, byte_count=size)
            )
    return tuple(files), configuration_coverage(files)


def read_manifest(root: Path, identity: DatabaseIdentity) -> BackupManifest:
    manifest = BackupManifest.model_validate_json(state_path(root, "backup.json").read_bytes())
    if (manifest.kb_id, manifest.generation) != (identity.kb_id, identity.generation):
        raise ValueError("Backup manifest belongs to another database generation")
    if len({file.path for file in manifest.files}) != len(manifest.files):
        raise ValueError("Backup manifest repeats a file")
    for file in manifest.files:
        if fingerprint(state_path(root, file.path)) != (file.digest, file.byte_count):
            raise ValueError("Backup state failed digest verification")
    if len({item.view_id for item in manifest.worktrees}) != len(manifest.worktrees):
        raise ValueError("Backup repeats a paused worktree")
    for item in manifest.worktrees:
        paths = [change.path for change in item.changes]
        if len(set(paths)) != len(paths) or set(paths) & set(item.removed):
            raise ValueError("Backup repeats a working-copy change")
        validate_file_tree(paths)
        for change in item.changes:
            if fingerprint(worktree_payload(root, item.edit_id, change.path)) != (
                change.digest,
                change.byte_count,
            ):
                raise ValueError("Backup working-copy change failed digest verification")
    if len({p.name for p in manifest.pending_receipts}) != len(manifest.pending_receipts):
        raise ValueError("Backup repeats a pending receipt")
    for packet in manifest.pending_receipts:
        if fingerprint(pending_payload(root, packet.name)) != (packet.digest, packet.byte_count):
            raise ValueError("Backup pending receipt failed digest verification")
    return manifest


def worktree_payload(root: Path, edit_id: str, path: str) -> Path:
    return state_path(root, f".ultrakb/backup-state/wiki/{edit_id}/{path}")


def pending_payload(root: Path, name: str) -> Path:
    return state_path(root, f".ultrakb/receipts/pending/{name}")


def restore_configuration(backup: Path, destination: Path, manifest: BackupManifest, copy_verified):
    for file in manifest.files:
        copy_verified(state_path(backup, file.path), destination / file.path, file.digest)
