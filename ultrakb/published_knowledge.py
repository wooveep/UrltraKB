"""Durable public pointers to immutable knowledge and their frozen read closure.

Working files commit first. Only a committed journal may switch the public
pointer; retrying that switch never runs compilation or rolls files back.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from pydantic import TypeAdapter

from ultrakb.ingest_records import KnowledgeHead, KnowledgeRevision
from ultrakb.locks import (
    DelegatedWriteLease,
    _fsync_directory,
    atomic_write_json,
    kb_ingest_lock_held,
)
from ultrakb.mutation import RecoveryRequired
from ultrakb.source_records import Record, RecordId, RelativePath, ViewId

if TYPE_CHECKING:
    from ultrakb.mutation import MutationSnapshot


class PublicationUnavailable(ValueError):
    """No matching public snapshot exists; a reader must not use working files."""


class PublicationPending(RecoveryRequired):
    """Internal changes committed, but public publication still needs recovery."""

    def __init__(self, publication_id: str, journal_path: Path):
        self.publication_id = publication_id
        self.journal_path = journal_path
        super().__init__(
            f"Changes committed; publication {publication_id} needs recovery ({journal_path})"
        )


class PublicRecord(Record):
    publication_id: RecordId
    generation: str
    view_id: ViewId = "legacy"
    knowledge_revision_id: RecordId
    head: KnowledgeHead
    manifest: KnowledgeRevision
    fingerprint: str
    metadata_files: dict[RelativePath, str]


class PendingPublication(Record):
    publication_id: RecordId
    transaction_id: RecordId
    expected_publication_id: RecordId | None
    generation: str


@dataclass(frozen=True)
class PublishedKnowledge:
    kb_dir: Path
    directory: Path
    record: PublicRecord

    @property
    def publication_id(self) -> str:
        return self.record.publication_id

    @property
    def generation(self) -> str:
        return self.record.generation

    @property
    def view_id(self) -> str:
        return self.record.view_id

    @property
    def knowledge_revision_id(self) -> str:
        return self.record.knowledge_revision_id

    @property
    def head(self) -> KnowledgeHead:
        return self.record.head

    @property
    def manifest(self) -> KnowledgeRevision:
        return self.record.manifest

    @property
    def wiki_dir(self) -> Path:
        return self.revision_dir / "wiki"

    @property
    def revision_dir(self) -> Path:
        return (
            self.kb_dir
            / ".ultrakb/knowledge"
            / self.view_id
            / "revisions"
            / self.knowledge_revision_id
        )

    @property
    def metadata_dir(self) -> Path:
        return self.directory / "metadata"

    @property
    def index_dir(self) -> Path:
        return self.revision_dir / "index"


_transaction: ContextVar[MutationSnapshot | None] = ContextVar("public_transaction", default=None)


def current_pointer(kb_dir: Path) -> Path:
    return kb_dir / ".ultrakb/published/current.json"


def recover_before_mutation(
    kb_dir: Path, *, delegated_lease: DelegatedWriteLease | None = None
) -> None:
    """A second write in the same lease must also finish a pending public switch."""
    from ultrakb.mutation import recover_pending_journals

    previous = _transaction.get()
    if previous is None or not previous.publication_active or previous.kb_dir != kb_dir.resolve():
        recover_pending_journals(kb_dir, delegated_lease=delegated_lease)


def _directory(kb_dir: Path, identity: str) -> Path:
    TypeAdapter(RecordId).validate_python(identity)
    return kb_dir / ".ultrakb/published/records" / identity


def _pointer_id(kb_dir: Path) -> str | None:
    path = current_pointer(kb_dir)
    if not path.exists():
        return None
    data = json.loads(path.read_text("utf-8"))
    return TypeAdapter(RecordId).validate_python(data["publication_id"])


def read_publication(
    kb_dir: Path, publication_id: str | None = None, *, view_id: str = "legacy"
) -> PublishedKnowledge:
    """Pin one complete public identity without taking the writer's lease."""
    root = kb_dir.expanduser().resolve()
    try:
        current = _pointer_id(root)
        identity = publication_id if publication_id is not None else current
        if identity is None:
            raise PublicationUnavailable("Public knowledge is not initialized; open it for writing")
        result = _sealed_publication(root, identity, view_id=view_id)
        if current != identity:
            receipt = json.loads((result.directory / "receipt.json").read_text("utf-8"))
            if receipt.get("status") != "published":
                raise PublicationUnavailable("Requested knowledge has not been made public")
        return result
    except (OSError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, PublicationUnavailable):
            raise
        raise PublicationUnavailable("Requested public knowledge is unavailable") from exc


def _sealed_publication(
    root: Path, identity: str, *, view_id: str = "legacy"
) -> PublishedKnowledge:
    from ultrakb.lifecycle import current_generation

    directory = _directory(root, identity)
    record = PublicRecord.model_validate_json((directory / "record.json").read_text("utf-8"))
    if (
        record.publication_id != identity
        or record.generation != current_generation(root)
        or record.view_id != view_id
        or record.head.knowledge_revision_id != record.knowledge_revision_id
        or record.manifest.knowledge_revision_id != record.knowledge_revision_id
    ):
        raise PublicationUnavailable("Public knowledge identity or generation does not match")
    result = PublishedKnowledge(root, directory, record)
    if not result.wiki_dir.is_dir():
        raise PublicationUnavailable("Public knowledge revision is missing")
    return result


@contextmanager
def publication_transaction(snapshot: MutationSnapshot) -> Iterator[bool]:
    """Only the outer mutation owns the publication and its commit evidence."""
    previous = _transaction.get()
    outer = True
    if previous is not None and previous.kb_dir == snapshot.kb_dir:
        with previous.journal_guard:
            outer = not previous.publication_active
            if not outer:
                snapshot.parent_journal = previous.journal_path.name
                snapshot.write_journal("active")
                previous.absorb(snapshot)
    token = _transaction.set(snapshot) if outer else None
    snapshot.publication_active = True
    try:
        yield outer
        if not outer and previous is not None and snapshot.cleanup_paths:
            previous.schedule_cleanup([snapshot.kb_dir / path for path in snapshot.cleanup_paths])
    finally:
        with snapshot.journal_guard:
            snapshot.publication_active = False
        if token is not None:
            _transaction.reset(token)


def validate_checkpoint_paths(kb_dir: Path, paths: list[Path]) -> None:
    """Independent computation must never escape or overlap a publication's preimages."""
    root = kb_dir.resolve()
    private = root / ".ultrakb/index-checkpoints"
    previous = _transaction.get()
    for path in paths:
        target = path.resolve()
        if target == private or not target.is_relative_to(private):
            raise ValueError("Independent checkpoint requires private index-checkpoint paths")
        if (
            previous is not None
            and previous.kb_dir == root
            and any(
                target.is_relative_to(parent) or parent.is_relative_to(target)
                for parent in previous.entries
            )
        ):
            raise ValueError("Independent checkpoint overlaps an enclosing mutation")


@contextmanager
def knowledge_operation(
    kb_dir: Path, *, operation: str, paths: tuple[Path, ...] = ()
) -> Iterator[None]:
    """Give a multi-mutation Wiki use case one commit and one public switch."""
    from ultrakb.mutation import mutation_scope

    root = kb_dir.resolve()
    wiki = root / "wiki"
    with mutation_scope(
        root,
        [wiki, root / ".ultrakb/knowledge/legacy/head.json", *paths],
        operation=operation,
        hardlink_dirs={wiki},
    ):
        yield


def _ensure_revision(snapshot: MutationSnapshot) -> tuple[KnowledgeHead, KnowledgeRevision]:
    from ultrakb.knowledge_evidence import publication_dependencies, publication_originals
    from ultrakb.source_catalog import write_record
    from ultrakb.unit_publication import copy_tree, read_head, wiki_versions

    root, wiki = snapshot.kb_dir, snapshot.kb_dir / "wiki"
    head = read_head(root)
    pages = wiki_versions(root, wiki)
    previous = None
    if head.knowledge_revision_id:
        directory = root / ".ultrakb/knowledge/legacy/revisions" / head.knowledge_revision_id
        previous = KnowledgeRevision.model_validate_json((directory / "manifest.json").read_text())
        if wiki_versions(root, directory / "wiki") == pages:
            return head, previous
    identity = uuid.uuid4().hex
    directory = root / ".ultrakb/knowledge/legacy/revisions" / identity
    head_path = root / ".ultrakb/knowledge/legacy/head.json"
    snapshot.capture([head_path])
    snapshot.track_new([directory])
    dependencies = publication_dependencies(root, head, pages, pages, head.inputs.values())
    originals = publication_originals(
        root, "legacy", head.inputs.values(), dependencies, wiki, pages
    )
    manifest = KnowledgeRevision(
        knowledge_revision_id=identity,
        view_id="legacy",
        base_revision_id=head.knowledge_revision_id,
        change_kind="manual",
        input_revisions=tuple(sorted(head.inputs.values())),
        page_dependencies=dependencies,
        generated_baselines=head.generated_baselines,
        original_references=originals,
    )
    copy_tree(wiki, directory / "wiki")
    write_record(directory / "manifest.json", manifest)
    head = head.model_copy(
        update={"generation": head.generation + 1, "knowledge_revision_id": identity}
    )
    write_record(head_path, head)
    return head, manifest


def _metadata_paths(root: Path) -> list[Path]:
    """Include read inputs, never the active context database or private settings."""
    paths = []
    for relative in (
        ".ultrakb/catalog/sources",
        ".ultrakb/catalog/source-revisions",
        ".ultrakb/catalog/units",
        ".ultrakb/catalog/unit-revisions",
        ".ultrakb/catalog/publications",
        ".ultrakb/catalog/normalizations",
        ".ultrakb/catalog/workbooks",
        ".ultrakb/artifacts",
        ".ultrakb/normalized",
        ".ultrakb/retained-documents",
        "raw",
    ):
        directory = root / relative
        if directory.is_symlink():
            raise ValueError("Publication resources must be regular files")
        if directory.exists():
            for path in directory.rglob("*"):
                if path.is_symlink():
                    # Raw inbox entries are untrusted and may change after admission.
                    # Only the admitted artifact can establish retained source identity.
                    if relative == "raw":
                        continue
                    raise ValueError("Publication resources must be regular files")
                if path.is_file():
                    paths.append(path)
    for relative in (".ultrakb/hashes.json", ".ultrakb/format.json"):
        path = root / relative
        if path.is_file():
            paths.append(path)
    return sorted(paths)


def _retain_file(source: Path, target: Path, resources: Path, digest: str) -> None:
    from ultrakb.mutation import _copy_file_atomic

    resource = resources / digest
    if not resource.exists():
        _copy_file_atomic(source, resource)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(resource, target)
        _fsync_directory(target.parent)
    except OSError:
        if target.exists():
            raise
        _copy_file_atomic(resource, target)


def _seal_directories(root: Path, directory: Path) -> None:
    for path in sorted(
        (path for path in directory.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        _fsync_directory(path)
    for path in (directory, *directory.parents):
        _fsync_directory(path)
        if path == root:
            break


def _publication_relevant(snapshot: MutationSnapshot) -> bool:
    if snapshot.operation == "initialize-publication":
        return True
    readable = [snapshot.kb_dir / "wiki", snapshot.kb_dir / "raw"]
    readable += [
        snapshot.kb_dir / ".ultrakb" / name
        for name in (
            "knowledge",
            "hashes.json",
            "artifacts",
            "normalized",
            "retained-documents",
            "catalog/sources",
            "catalog/source-revisions",
            "catalog/units",
            "catalog/unit-revisions",
            "catalog/publications",
            "catalog/normalizations",
            "catalog/workbooks",
        )
    ]
    return any(
        target == path or target.is_relative_to(path) or path.is_relative_to(target)
        for target in snapshot.entries
        for path in readable
    )


def prepare_publication(snapshot: MutationSnapshot) -> None:
    """Seal a complete closure and persist its intent before internal commit."""
    from ultrakb.catalog_schema import CatalogSchema, catalog_schema_path
    from ultrakb.kb_format import require_current_kb
    from ultrakb.lifecycle import current_generation
    from ultrakb.source_catalog import write_record
    from ultrakb.state import HashRegistry

    root = snapshot.kb_dir
    if not _publication_relevant(snapshot):
        return
    if not (root / ".ultrakb/config.yaml").is_file() or not (root / "wiki").is_dir():
        return
    require_current_kb(root)
    schema = catalog_schema_path(root)
    if (
        not schema.exists()
        or CatalogSchema.model_validate_json(schema.read_text()) != CatalogSchema()
    ):
        snapshot.capture([schema])
        write_record(schema, CatalogSchema())
    head, manifest = _ensure_revision(snapshot)
    paths = _metadata_paths(root)
    files = {p.relative_to(root).as_posix(): HashRegistry.hash_file(p) for p in paths}
    fingerprint = hashlib.sha256(
        json.dumps([head.model_dump(mode="json"), files], sort_keys=True).encode()
    ).hexdigest()
    expected = _pointer_id(root)
    generation = current_generation(root)
    if expected:
        prior = PublicRecord.model_validate_json(
            (_directory(root, expected) / "record.json").read_text("utf-8")
        )
        if prior.generation == generation:
            if read_publication(root, expected).record.fingerprint == fingerprint:
                return
        elif snapshot.operation != "initialize-publication":
            raise PublicationUnavailable("Initialize the copied knowledge base before writing")
    identity = uuid.uuid4().hex
    directory = _directory(root, identity)
    snapshot.track_new([directory])
    directory.mkdir(parents=True, exist_ok=False)
    for source in paths:
        relative = source.relative_to(root)
        _retain_file(
            source,
            directory / "metadata" / relative,
            root / ".ultrakb/published/resources",
            files[relative.as_posix()],
        )
    record = PublicRecord(
        publication_id=identity,
        generation=generation,
        knowledge_revision_id=manifest.knowledge_revision_id,
        head=head,
        manifest=manifest,
        fingerprint=fingerprint,
        metadata_files=files,
    )
    atomic_write_json(directory / "record.json", record.model_dump(mode="json"))
    _seal_directories(root, directory)
    revision = root / ".ultrakb/knowledge/legacy/revisions" / manifest.knowledge_revision_id
    _seal_directories(root, revision)
    snapshot.publication = PendingPublication(
        publication_id=identity,
        transaction_id=snapshot.journal_path.stem,
        expected_publication_id=expected,
        generation=record.generation,
    ).model_dump(mode="json")
    snapshot.write_journal("active")


def publish_committed(
    snapshot: MutationSnapshot, *, delegated_lease: DelegatedWriteLease | None = None
) -> bool:
    """Linearize a committed result; every later failure leaves it committed."""
    from ultrakb.lifecycle import current_generation

    if snapshot.publication is None:
        return True
    pending = PendingPublication.model_validate(snapshot.publication)
    root = snapshot.kb_dir
    if not kb_ingest_lock_held(root / ".ultrakb") and not (
        delegated_lease is not None and delegated_lease.authorizes(root / ".ultrakb/ingest.lock")
    ):
        raise RuntimeError("Publication requires the knowledge-base write lease")
    try:
        journal = json.loads(snapshot.journal_path.read_text("utf-8"))
        if journal["status"] != "committed" or pending.transaction_id != snapshot.journal_path.stem:
            raise ValueError("Publication transaction has not committed")
        if current_generation(root) != pending.generation:
            raise ValueError("Publication belongs to an obsolete knowledge-base generation")
        current = _pointer_id(root)
        if current != pending.publication_id:
            if current != pending.expected_publication_id:
                raise ValueError("Public knowledge changed before publication")
            # The sealed record must be readable before the pointer can name it.
            _sealed_publication(root, pending.publication_id)
            atomic_write_json(
                current_pointer(root),
                {
                    "publication_id": pending.publication_id,
                    "generation": pending.generation,
                    "view_id": "legacy",
                },
            )
            from ultrakb.execution_timing import record_phase_event

            record_phase_event("public_switch", stage="publication", work_id=pending.publication_id)
    except Exception as exc:
        raise PublicationPending(pending.publication_id, snapshot.journal_path) from exc
    try:
        atomic_write_json(
            _directory(root, pending.publication_id) / "receipt.json",
            {
                "transaction_id": pending.transaction_id,
                "status": "published",
            },
        )
        for relative in snapshot.cleanup_paths:
            TypeAdapter(RelativePath).validate_python(relative)
            if not relative.startswith(".ultrakb/compile-checkpoints/"):
                raise ValueError("Invalid post-publication checkpoint path")
            path = root / relative
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError("Post-publication cleanup cannot follow symbolic links")
            if path.exists():
                shutil.rmtree(path)
                _fsync_directory(path.parent)
    except Exception:
        logging.getLogger(__name__).warning(
            "Knowledge is public; receipt or cleanup will be recovered from %s",
            snapshot.journal_path,
        )
        return False
    return True


def initialize_publication(kb_dir: Path) -> PublishedKnowledge:
    """Explicit writer initialization for a supported KB without a public baseline."""
    from ultrakb.lifecycle import current_generation
    from ultrakb.mutation import mutation_scope

    if not kb_ingest_lock_held(kb_dir / ".ultrakb"):
        raise RuntimeError("Publication initialization requires the KB write lease")
    current = _pointer_id(kb_dir)
    stale_generation = False
    if current is not None:
        prior = PublicRecord.model_validate_json(
            (_directory(kb_dir, current) / "record.json").read_text("utf-8")
        )
        stale_generation = prior.generation != current_generation(kb_dir)
    if current is None or stale_generation:
        with mutation_scope(kb_dir, [], operation="initialize-publication"):
            pass
    return read_publication(kb_dir)
