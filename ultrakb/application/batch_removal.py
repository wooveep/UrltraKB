"""Aggregate Source withdrawal effects and execute reviewed per-source transactions."""

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from ultrakb.application.document_actions import DocumentIdentity
from ultrakb.application.execution import ExecutionContext
from ultrakb.application.removal import preview_removal, remove_document
from ultrakb.application.source_removal import preview_source_removal
from ultrakb.application.views import view_scope
from ultrakb.artifact_references import list_artifact_references
from ultrakb.file_state import file_versions
from ultrakb.ingest_records import KnowledgeRevision, UnitRevision
from ultrakb.knowledge_scope import resolve_scope
from ultrakb.locks import LockCancelled, kb_ingest_lock, kb_read_lock
from ultrakb.source_catalog import read_record, read_source, read_source_revision
from ultrakb.source_changes import effective_input, exclude_source_inputs, source_change_heads
from ultrakb.unit_publication import read_head


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

    def __post_init__(self):
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


class BatchRemovalChanged(ValueError):
    pass


def _version(root, scope, identities):
    paths = [
        root / ".ultrakb/catalog",
        root / ".ultrakb/knowledge" / scope.view_id,
        root / ".ultrakb/artifacts",
        scope.wiki_dir,
        root / ".ultrakb/hashes.json",
        root / "raw",
        root / ".ultrakb/context.sqlite",
        root / ".ultrakb/files",
    ]
    return hashlib.sha256(
        json.dumps([identities, file_versions(root, paths)], sort_keys=True).encode()
    ).hexdigest()


def preview_batch_removal(
    kb_dir: Path, identities, *, scope=None, keep_raw=False, keep_empty=False
) -> BatchRemovalPreview:
    root = kb_dir.expanduser().resolve()
    selected = tuple(sorted(set(identities)))
    if not selected or any(
        not isinstance(identity, DocumentIdentity) or identity.kind not in {"source", "legacy"}
        for identity in selected
    ):
        raise ValueError("批量移除需要明确的新旧资料身份。")
    view = resolve_scope(root, scope, writable=True)
    with kb_read_lock(root / ".ultrakb"):
        sources = [
            read_source(root, identity.value) for identity in selected if identity.kind == "source"
        ]
        from ultrakb.application.legacy_removal_effects import legacy_effects

        legacy = legacy_effects(root, selected, view, keep_raw=keep_raw, keep_empty=keep_empty)
        if any(source.removed for source in sources):
            raise BatchRemovalChanged("所选资料已移除，请重新选择。")
        affected: set[str] = set()
        for source in sources:
            changed = exclude_source_inputs(root, source, "withdrawn").model_copy(
                update={"removed": True, "target_generation": source.target_generation + 1}
            )
            for head in source_change_heads(root, changed, "withdrawn").values():
                affected.update(
                    page
                    for page, reasons in head.needs_refresh.items()
                    if any(reason.source_id == source.source_id for reason in reasons)
                )
        head = read_head(root, view.view_id)
        manifest = None
        if head.knowledge_revision_id:
            path = (
                root
                / ".ultrakb/knowledge"
                / view.view_id
                / "revisions"
                / head.knowledge_revision_id
                / "manifest.json"
            )
            manifest = KnowledgeRevision.model_validate_json(path.read_text())
        inputs = set(head.inputs.values())
        if manifest:
            inputs.update(
                identity for deps in manifest.page_dependencies.values() for identity in deps
            )
        active, selected_inputs = set(), set()
        source_ids = {source.source_id for source in sources}
        for identity in inputs:
            unit = read_record(root, "unit-revisions", identity, UnitRevision)
            frozen = read_source_revision(root, unit.source_revision_id)
            if effective_input(root, view.view_id, identity):
                active.add(identity)
            if frozen.source_id in source_ids:
                selected_inputs.add(identity)
        lose_all = tuple(
            sorted(
                page
                for page in affected
                if manifest
                and set(manifest.page_dependencies.get(page, ())) & active
                and not set(manifest.page_dependencies.get(page, ())) & (active - selected_inputs)
            )
        )
        references = list_artifact_references(root)
        originals = tuple(
            sorted(
                {
                    read_source_revision(root, source.target_revision_id).original
                    for source in sources
                }
            )
        )
        retained = tuple(
            sorted(
                str(path.relative_to(root))
                for path in references.paths
                if path.is_relative_to(root)
            )
        )
        return BatchRemovalPreview(
            str(root),
            view.view_id,
            selected,
            tuple(
                next(
                    f"{source.name} · {source.identity}"
                    for source in sources
                    if source.source_id == item.value
                )
                if item.kind == "source"
                else legacy.names[item.value]
                for item in selected
            ),
            _version(root, view, [asdict(identity) for identity in selected]),
            tuple(sorted(affected)),
            lose_all,
            originals,
            retained,
            references.complete,
            keep_raw,
            keep_empty,
            legacy.deleted,
            legacy.modified,
            legacy.retained,
            legacy.empty_pages,
        )


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
            "请分别确认新版来源撤下和旧资料清理后果。",
        )
    try:
        context.check_stop()
        return _execute_with_lease(preview, context=context)
    except LockCancelled:
        return BatchRemovalOutcome(
            "stopped",
            tuple(
                BatchRemovalItem(item.kind, item.value, name, "not_run", "已停止；未执行。")
                for item, name in zip(preview.identities, preview.names, strict=True)
            ),
        )


def _execute_with_lease(preview: BatchRemovalPreview, *, context) -> BatchRemovalOutcome:
    root = Path(preview.kb_dir)
    view = resolve_scope(root, view_scope(root, preview.view_id), writable=True)
    items = []
    with kb_ingest_lock(root / ".ultrakb", cancelled=context.cancelled, on_wait=context.waiting):
        fresh = preview_batch_removal(
            root,
            preview.identities,
            scope=view,
            keep_raw=preview.keep_raw,
            keep_empty=preview.keep_empty,
        )
        if fresh != preview:
            return BatchRemovalOutcome(
                "blocked",
                tuple(
                    BatchRemovalItem(item.kind, item.value, name, "not_run", "确认已过期；未执行。")
                    for item, name in zip(preview.identities, preview.names, strict=True)
                ),
                "资料、引用或知识已改变；请重新查看聚合影响并确认。",
            )
        expected = preview.version
        for index, identity in enumerate(preview.identities):
            try:
                context.check_stop()
                if _version(root, view, [asdict(item) for item in preview.identities]) != expected:
                    raise BatchRemovalChanged("批内检测到未批准变化，剩余项停止；已完成移除保留。")
                remaining = preview_batch_removal(
                    root,
                    preview.identities[index:],
                    scope=view,
                    keep_raw=preview.keep_raw,
                    keep_empty=preview.keep_empty,
                )
                if not set(remaining.affected_pages).issubset(preview.affected_pages) or not set(
                    remaining.lose_all_sources
                ).issubset(preview.lose_all_sources):
                    raise BatchRemovalChanged("剩余项影响超出批准范围，请重新预览。")
                if not set(remaining.legacy_deleted).issubset(preview.legacy_deleted) or not set(
                    remaining.legacy_modified
                ).issubset(set(preview.legacy_modified) | set(preview.legacy_deleted)):
                    raise BatchRemovalChanged("旧资料清理后果超出批准范围，请重新预览。")
                single = (
                    preview_source_removal(root, identity.value, view)
                    if identity.kind == "source"
                    else preview_removal(
                        root,
                        identity.value,
                        scope=view,
                        keep_raw=preview.keep_raw,
                        keep_empty=preview.keep_empty,
                    )
                )
                if single is None or single.status != "ready":
                    raise BatchRemovalChanged("资料目标已改变，请重新预览。")
                outcome = remove_document(
                    root,
                    identity.value,
                    version=single.version,
                    context=context,
                    scope=view,
                    keep_raw=preview.keep_raw if identity.kind == "legacy" else False,
                    keep_empty=preview.keep_empty if identity.kind == "legacy" else False,
                )
                if outcome.status != "removed":
                    if outcome.result and outcome.result.changes:
                        items.append(
                            BatchRemovalItem(
                                identity.kind,
                                identity.value,
                                preview.names[index],
                                "partial",
                                "部分清理已提交；请核对结果，剩余项停止。",
                                outcome.retained,
                                outcome.result.changes,
                                outcome.unfinished,
                            )
                        )
                    raise BatchRemovalChanged("本项未完成，剩余移除已停止。")
                items.append(
                    BatchRemovalItem(
                        identity.kind,
                        identity.value,
                        preview.names[index],
                        "completed",
                        "已撤下有效贡献；原件和历史保留，相关知识待更新。"
                        if identity.kind == "source"
                        else "已按批准范围清理旧资料；旧链接可能不可读。",
                        outcome.retained,
                        outcome.result.changes if outcome.result else (),
                        outcome.unfinished,
                    )
                )
                expected = _version(root, view, [asdict(item) for item in preview.identities])
                context.on_event(
                    {
                        "stage": "removing",
                        "source": preview.names[index],
                        "completed": len(items),
                        "total": len(preview.identities),
                    }
                )
            except LockCancelled:
                tail = [
                    BatchRemovalItem(
                        item.kind, item.value, preview.names[i], "not_run", "已停止；未执行。"
                    )
                    for i, item in enumerate(preview.identities[len(items) :], len(items))
                ]
                return BatchRemovalOutcome("stopped", tuple(items + tail))
            except Exception as error:
                from ultrakb.ingest_diagnostics import failure_reason

                if len(items) == index:
                    items.append(
                        BatchRemovalItem(
                            identity.kind,
                            identity.value,
                            preview.names[index],
                            "failed",
                            failure_reason(error),
                        )
                    )
                items.extend(
                    BatchRemovalItem(
                        item.kind,
                        item.value,
                        preview.names[i],
                        "not_run",
                        "前项失败或影响改变；未执行。",
                    )
                    for i, item in enumerate(preview.identities[len(items) :], len(items))
                )
                return BatchRemovalOutcome(
                    "partial"
                    if any(item.status in {"completed", "partial"} for item in items)
                    else "blocked",
                    tuple(items),
                    failure_reason(error),
                    type(error).__name__ == "RecoveryRequired"
                    or any("knowledge_base_repair" in item.unfinished for item in items),
                )
    return BatchRemovalOutcome("completed", tuple(items))
