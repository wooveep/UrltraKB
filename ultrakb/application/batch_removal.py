"""Aggregate Source source_change effects and execute reviewed per-source transactions."""

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from ultrakb.application.document_actions import DocumentIdentity
from ultrakb.application.execution import ExecutionContext
from ultrakb.knowledge_scope import resolve_scope
from ultrakb.locks import LockCancelled


@dataclass(frozen=True)
class BatchRemovalPreview:
    kb_dir: str
    view_id: str
    identities: tuple[DocumentIdentity, ...]
    names: tuple[str, ...]
    version: str
    affected_pages: tuple[str, ...]
    lose_all_sources: tuple[str, ...]
    originals: tuple[str, ...]
    retained: tuple[str, ...]
    references_complete: bool
    keep_raw: bool = False
    keep_empty: bool = False
    legacy_deleted: tuple[str, ...] = ()
    legacy_modified: tuple[str, ...] = ()
    legacy_retained: tuple[str, ...] = ()
    legacy_empty: tuple[str, ...] = ()
    storage_kb_id: str | None = None
    storage_generation: str | None = None
    publication_id: str | None = None
    source_versions: tuple[tuple[str, str, int], ...] = ()

    def __post_init__(self):
        if any(
            value is not None
            for value in (self.storage_kb_id, self.storage_generation, self.publication_id)
        ):
            from pydantic import TypeAdapter

            from ultrakb.source_records import RecordId
            from ultrakb.storage.source_changes import SourceChange

            if any(item.kind != "source" for item in self.identities):
                raise ValueError("Main-store removal requires retained source identities")
            for value in (self.storage_kb_id, self.storage_generation, self.publication_id):
                TypeAdapter(RecordId).validate_python(value)
            for source_id, revision_id, generation in self.source_versions:
                SourceChange(
                    source_id=source_id,
                    source_revision_id=revision_id,
                    source_generation=generation,
                )
            if tuple(item[0] for item in self.source_versions) != tuple(
                item.value for item in self.identities
            ):
                raise ValueError("Reviewed storage removal requires its exact source versions")
        elif self.source_versions:
            raise ValueError("Source versions require an explicit database identity")
        if not self.identities or len(self.identities) != len(self.names):
            raise ValueError("Invalid reviewed removal targets")
        if any(item.kind not in {"source", "legacy"} for item in self.identities):
            raise ValueError("Invalid reviewed removal kind")
        if type(self.keep_raw) is not bool or type(self.keep_empty) is not bool:
            raise ValueError("Invalid legacy retention options")
        if len(set(self.identities)) != len(self.identities) or not self.version:
            raise ValueError("Invalid reviewed removal identities or version")
        if type(self.references_complete) is not bool or any(
            not isinstance(value, str) or not value
            for values in (
                self.names,
                self.affected_pages,
                self.lose_all_sources,
                self.originals,
                self.retained,
            )
            for value in values
        ):
            raise ValueError("Invalid reviewed removal facts")

    def encode(self):
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def decode(cls, value):
        raw = json.loads(value)
        raw["identities"] = tuple(DocumentIdentity(**item) for item in raw["identities"])
        raw["source_versions"] = tuple(tuple(item) for item in raw.get("source_versions", ()))
        for key in (
            "names",
            "affected_pages",
            "lose_all_sources",
            "originals",
            "retained",
            "legacy_deleted",
            "legacy_modified",
            "legacy_retained",
            "legacy_empty",
        ):
            raw.setdefault(key, ())
            raw[key] = tuple(raw[key])
        return cls(**raw)


@dataclass(frozen=True)
class BatchRemovalItem:
    kind: str
    identifier: str
    name: str
    status: str
    message: str | None = None
    retained: tuple[str, ...] = ()
    changes: tuple[str, ...] = ()
    unfinished: tuple[str, ...] = ()

    def __post_init__(self):
        if self.kind not in {"source", "legacy"} or self.status not in {
            "completed",
            "failed",
            "not_run",
            "partial",
        }:
            raise ValueError("Invalid removal outcome")
        if any(not isinstance(value, str) or not value for value in (self.identifier, self.name)):
            raise ValueError("Invalid removal identity")
        if not isinstance(self.retained, tuple) or any(
            not isinstance(value, str) for value in self.retained
        ):
            raise ValueError("Invalid retained removal resources")
        if any(
            not isinstance(values, tuple) or any(not isinstance(value, str) for value in values)
            for values in (self.changes, self.unfinished)
        ):
            raise ValueError("Invalid removal change facts")


@dataclass(frozen=True)
class BatchRemovalOutcome:
    status: str
    items: tuple[BatchRemovalItem, ...]
    message: str | None = None
    needs_repair: bool = False
    publication_id: str | None = None


class BatchRemovalChanged(ValueError):
    pass


def preview_batch_removal(
    kb_dir: Path, identities, *, scope=None, keep_raw=False, keep_empty=False
) -> BatchRemovalPreview:
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")
    root = kb_dir.expanduser().resolve()
    selected = tuple(sorted(set(identities)))
    if not selected or any(
        not isinstance(identity, DocumentIdentity) or identity.kind != "source"
        for identity in selected
    ):
        raise ValueError("批量撤下需要明确的主库来源身份。")
    from .storage_batch_removal import preview_stored_batch

    view = resolve_scope(root, scope)
    return preview_stored_batch(view, selected, keep_raw=keep_raw, keep_empty=keep_empty)


def execute_batch_removal(
    preview: BatchRemovalPreview, *, context=None, confirmed_groups=("source",)
) -> BatchRemovalOutcome:
    context = context or ExecutionContext()
    if not {item.kind for item in preview.identities}.issubset(confirmed_groups):
        return BatchRemovalOutcome(
            "blocked",
            tuple(
                BatchRemovalItem(
                    item.kind, item.value, name, "not_run", "移除后果尚未分别确认；未执行。"
                )
                for item, name in zip(preview.identities, preview.names, strict=True)
            ),
            "请确认来源撤下后果。",
        )
    try:
        context.check_stop()
        if preview.storage_kb_id is not None:
            from .storage_batch_removal import execute_stored_batch

            return execute_stored_batch(preview, context)
        raise BatchRemovalChanged("A main-store removal requires its reviewed database identity")
    except LockCancelled:
        return BatchRemovalOutcome(
            "stopped",
            tuple(
                BatchRemovalItem(item.kind, item.value, name, "not_run", "已停止；未执行。")
                for item, name in zip(preview.identities, preview.names, strict=True)
            ),
        )
