"""Review an exact typed selection and recheck it before creating a document task."""

from dataclasses import dataclass, replace
from pathlib import Path

from ultrakb.application.document_actions import DocumentIdentity, document_inventory
from ultrakb.application.recompilation import RecompileTarget, _version, select_recompilation
from ultrakb.knowledge_scope import KnowledgeScope
from ultrakb.locks import kb_read_lock


@dataclass(frozen=True)
class DocumentSkip:
    identity: DocumentIdentity
    name: str
    reason: str


@dataclass(frozen=True)
class DocumentRetryTarget:
    identity: DocumentIdentity
    name: str
    source_revision_id: str
    unit_id: str | None = None
    unit_revision_id: str | None = None
    worksheet: bool = False


@dataclass(frozen=True)
class DocumentBatchPreview:
    kb_dir: Path
    selected: tuple[DocumentIdentity, ...]
    approved: tuple[DocumentIdentity, ...]
    skipped: tuple[DocumentSkip, ...]
    version: str | None
    targets: tuple[RecompileTarget, ...]
    action: str = "recompile"
    retry_targets: tuple[DocumentRetryTarget, ...] = ()


class DocumentBatchChanged(ValueError):
    pass


def preview_document_batch(
    kb_dir: Path, identities, *, scope: KnowledgeScope | None = None, action="recompile"
) -> DocumentBatchPreview:
    root = kb_dir.expanduser().resolve()
    provided = tuple(identities)
    if not all(isinstance(identity, DocumentIdentity) for identity in provided):
        raise ValueError("Document batches require typed catalog identities")
    selected = tuple(sorted(set(provided)))
    if action not in {"recompile", "retry"}:
        raise ValueError("Unsupported document batch action")
    with kb_read_lock(root / ".ultrakb"):
        rows = {row.identity: row for row in document_inventory(root, scope=scope)}
        if action == "retry":
            approved, skipped, retry_targets = [], [], []
            for identity in selected:
                row = rows.get(identity)
                if row is None or not row.actions["retry"].allowed:
                    skipped.append(
                        DocumentSkip(
                            identity,
                            row.document["name"] if row else identity.value,
                            row.actions["retry"].reason if row else "资料已移除或不在当前视图。",
                        )
                    )
                    continue
                approved.append(identity)
                doc = row.document
                for unit in doc.get("units") or [None]:
                    if unit and unit.get("status") in {
                        "completed",
                        "empty",
                        "retired",
                        "awaiting_confirmation",
                    }:
                        continue
                    label = doc["name"] + (f" · {unit['name']}" if unit else "")
                    retry_targets.append(
                        DocumentRetryTarget(
                            identity,
                            label,
                            doc["target_source_revision_id"],
                            unit["unit_id"] if unit else None,
                            unit.get("target_revision_id") if unit else None,
                            bool(unit and unit.get("key") != "body"),
                        )
                    )
            return DocumentBatchPreview(
                root,
                selected,
                tuple(approved),
                tuple(skipped),
                _version(root, scope=scope),
                (),
                action,
                tuple(retry_targets),
            )
        selection = select_recompilation(root, all_docs=True, confirmation=True, scope=scope)
        approved, skipped, targets = [], [], []
        for identity in selected:
            row = rows.get(identity)
            matches = tuple(
                target for target in selection.targets if target.file_hash == identity.value
            )
            reason = (
                "资料已移除或不在当前视图。"
                if row is None
                else row.actions["recompile"].reason
                if not row.actions["recompile"].allowed
                else "资料标识冲突或已改变，请核对记录。"
                if len(matches) != 1
                else None
            )
            if reason is not None:
                skipped.append(
                    DocumentSkip(identity, row.document["name"] if row else identity.value, reason)
                )
            else:
                assert row is not None
                approved.append(identity)
                label = row.document["name"]
                if row.document.get("location"):
                    label += f"（{row.document['location']}）"
                targets.append(replace(matches[0], name=label))
        return DocumentBatchPreview(
            root, selected, tuple(approved), tuple(skipped), selection.version, tuple(targets)
        )


def validate_document_batch(preview: DocumentBatchPreview, *, scope=None) -> DocumentBatchPreview:
    current = preview_document_batch(
        preview.kb_dir, preview.selected, scope=scope, action=preview.action
    )
    if (current.version, current.approved, current.skipped, current.retry_targets) != (
        preview.version,
        preview.approved,
        preview.skipped,
        preview.retry_targets,
    ):
        raise DocumentBatchChanged("资料或知识已改变；请重新查看适用范围并确认。")
    return current
