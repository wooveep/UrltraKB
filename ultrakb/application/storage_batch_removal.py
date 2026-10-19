"""Review source exclusions against fixed input versions and advance only owned publications."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from ultrakb.locks import LockCancelled
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.database import DatabaseIdentity
from ultrakb.storage.knowledge_store import KnowledgeStore

from .batch_removal import (
    BatchRemovalChanged,
    BatchRemovalItem,
    BatchRemovalOutcome,
    BatchRemovalPreview,
)
from .removal import remove_document
from .storage_removal import _version
from .views import storage_scope


def preview_stored_batch(scope, selected, *, keep_raw, keep_empty):
    if any(item.kind != "source" for item in selected):
        raise ValueError("Main-store removal requires retained source identities")
    base = scope.knowledge
    catalog = CatalogStore(scope.kb_dir, base.identity)
    sources = [catalog.source(item.value) for item in selected]
    if any(source.removed for source in sources):
        raise BatchRemovalChanged("所选资料已退出，请重新选择。")
    return _preview(scope, selected, sources, keep_raw=keep_raw, keep_empty=keep_empty)


def _preview(scope, selected, sources, *, keep_raw, keep_empty):
    base = scope.knowledge
    catalog = CatalogStore(scope.kb_dir, base.identity)
    versions = tuple(
        (source.source_id, source.revision_id, source.generation) for source in sources
    )
    identity = (base.identity.kb_id, base.identity.generation, scope.view_id, scope.publication_id)
    version = hashlib.sha256(json.dumps((identity, versions)).encode()).hexdigest()
    selected_ids = {source.source_id for source in sources}
    inputs = {unit.unit_revision_id: unit.source_id for unit in base.input_units()}
    active = set(base.unit_revisions())
    excluded = {unit for unit, source in inputs.items() if source in selected_ids}
    affected, lose_all = [], []
    for path, units in base.page_input_revisions().items():
        if set(units) & excluded:
            affected.append(path)
            if set(units) & active and not (set(units) & active - excluded):
                lose_all.append(path)
    return BatchRemovalPreview(
        kb_dir=str(scope.kb_dir),
        view_id=scope.view_id,
        identities=selected,
        names=tuple(source.label for source in sources),
        version=version,
        affected_pages=tuple(sorted(affected)),
        lose_all_sources=tuple(sorted(lose_all)),
        originals=tuple(
            file.path
            for source in sources
            for file in catalog.revision(source.revision_id).files
            if file.kind == "main"
        ),
        retained=("wiki",),
        references_complete=True,
        keep_raw=keep_raw,
        keep_empty=keep_empty,
        storage_kb_id=base.identity.kb_id,
        storage_generation=base.identity.generation,
        publication_id=base.publication_id,
        source_versions=versions,
    )


def _completed(preview, index, outcome):
    return BatchRemovalItem(
        "source",
        preview.identities[index].value,
        preview.names[index],
        "completed",
        outcome.result.message,
        outcome.retained,
        (),
        outcome.unfinished,
    )


def execute_stored_batch(preview, context):
    items = []
    publication_id = None
    try:
        root = Path(preview.kb_dir)
        identity = DatabaseIdentity(preview.storage_kb_id, preview.storage_generation)
        scope = storage_scope(
            root, identity, publication_id=preview.publication_id, view_id=preview.view_id
        )
        original = scope
        store, catalog = KnowledgeStore(root, identity), CatalogStore(root, identity)
        sources = []
        for index, selected in enumerate(preview.identities):
            source = catalog.source(selected.value)
            _, reviewed_revision, reviewed_generation = preview.source_versions[index]
            if source.removed:
                if len(items) != index:
                    raise BatchRemovalChanged("退出顺序不属于本次已确认批次。")
                snapshot = replace(
                    source,
                    revision_id=reviewed_revision,
                    generation=reviewed_generation,
                    removed=False,
                )
                outcome = remove_document(
                    root,
                    selected.value,
                    scope=scope,
                    version=_version(scope, snapshot),
                    context=context,
                )
                if outcome.status != "removed":
                    raise BatchRemovalChanged("找不到本批次的已提交退出结果。")
                items.append(_completed(preview, index, outcome))
                publication_id = outcome.publication_id
                scope = storage_scope(
                    root, identity, publication_id=publication_id, view_id=scope.view_id
                )
                sources.append(snapshot)
            else:
                if (
                    source.source_id,
                    source.revision_id,
                    source.generation,
                ) != preview.source_versions[index]:
                    raise BatchRemovalChanged("资料或处理单元已改变，请重新预览。")
                sources.append(source)
        fresh = _preview(
            original,
            preview.identities,
            sources,
            keep_raw=preview.keep_raw,
            keep_empty=preview.keep_empty,
        )
        if fresh != preview:
            raise BatchRemovalChanged("资料或处理单元已改变，请重新预览。")
        for index in range(len(items), len(preview.identities)):
            selected = preview.identities[index]
            context.check_stop()
            source = catalog.source(selected.value)
            if (
                (source.source_id, source.revision_id, source.generation)
                != preview.source_versions[index]
                or source.removed
                or store.bind(view_id=scope.view_id).publication_id != scope.publication_id
            ):
                raise BatchRemovalChanged("确认后的资料或公开版本已改变，剩余项停止。")
            outcome = remove_document(
                root, selected.value, scope=scope, version=_version(scope, source), context=context
            )
            if outcome.status == "stopped":
                raise LockCancelled("Source withdrawal stopped before its publication")
            if outcome.status != "removed":
                raise BatchRemovalChanged("本项未完成，剩余移除已停止。")
            items.append(_completed(preview, index, outcome))
            publication_id = outcome.publication_id
            scope = storage_scope(
                root, identity, publication_id=outcome.publication_id, view_id=scope.view_id
            )
            context.on_event(
                {
                    "stage": "removing",
                    "source": preview.names[index],
                    "completed": len(items),
                    "total": len(preview.identities),
                }
            )
    except Exception as error:
        from ultrakb.ingest_diagnostics import failure_reason

        stopped = isinstance(error, LockCancelled)
        items.extend(
            BatchRemovalItem(
                item.kind,
                item.value,
                preview.names[index],
                "not_run",
                "已停止；未执行。" if stopped else "确认发生变化或前项未完成；未执行。",
            )
            for index, item in enumerate(preview.identities[len(items) :], len(items))
        )
        return BatchRemovalOutcome(
            "stopped"
            if stopped
            else "partial"
            if any(item.status == "completed" for item in items)
            else "blocked",
            tuple(items),
            failure_reason(error),
            publication_id=publication_id,
        )
    return BatchRemovalOutcome("completed", tuple(items), publication_id=publication_id)
