"""Project typed document identities and real command eligibility without model calls."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ultrakb.application.knowledge_bases import get_kb_list
from ultrakb.application.query_views import resolve_query_views
from ultrakb.documents import read_document_source
from ultrakb.knowledge_scope import KnowledgeScope, reading_scope, resolve_scope


@dataclass(frozen=True, order=True)
class DocumentIdentity:
    kind: Literal["source", "legacy"]
    value: str

    @classmethod
    def from_inventory(cls, document):
        # A Source's catalog ID identifies the document; its digest never does.
        # Legacy registries retain their existing key in a separate namespace.
        return (
            cls("source", document["source_id"])
            if document.get("source_id")
            else cls("legacy", document["hash"])
        )


@dataclass(frozen=True)
class Eligibility:
    allowed: bool
    reason: str = ""


@dataclass(frozen=True)
class DocumentRow:
    identity: DocumentIdentity
    document: dict
    status: str
    actions: dict[str, Eligibility]
    details: str


_STATUS = {
    "admitted": "等待处理",
    "pending": "等待处理",
    "running": "处理中",
    "processing": "处理中",
    "completed": "处理完成",
    "partial": "部分完成",
    "failed": "处理失败",
    "blocked": "待处理",
    "stopped": "已停止",
    "rejected": "原文不可用",
    "empty": "贡献已清空",
    "retired": "已停用",
    "removed": "已撤下",
    "awaiting_confirmation": "等待确认贡献",
}


def document_inventory(
    kb_dir: Path, *, scope: KnowledgeScope | None = None
) -> tuple[DocumentRow, ...]:
    """Use the same retained bodies and query snapshot as reading and asking."""
    scope = resolve_scope(kb_dir, scope)
    with reading_scope(scope):
        documents = get_kb_list(kb_dir, scope=scope)["documents"]
        source_ids = {doc["source_id"] for doc in documents if doc.get("source_id")}
        legacy_ids = {doc["hash"] for doc in documents if not doc.get("source_id")}
        selection = resolve_query_views(kb_dir, scope=scope)
        readable_files = {name for view in selection.views for name in view.files}
        readable_revisions = {r for view in selection.views for r in view.source_revision_ids}
        rows = []
        for doc in documents:
            doc = dict(doc)
            identity = DocumentIdentity.from_inventory(doc)
            if identity.kind == "source" and scope.knowledge is None:
                from ultrakb.source_catalog import read_source, read_source_revision

                if scope.publication is not None:
                    from ultrakb.source_catalog import read_record
                    from ultrakb.source_records import Source, SourceRevision

                    metadata = scope.publication.metadata_dir
                    source = read_record(metadata, "sources", identity.value, Source)
                    target = read_record(
                        metadata, "source-revisions", source.target_revision_id, SourceRevision
                    )
                else:
                    source = read_source(kb_dir, identity.value)
                    target = read_source_revision(kb_dir, source.target_revision_id)
                doc["location"] = source.identity
                doc["target_source_revision_id"] = source.target_revision_id
                doc["original_kind"] = target.original_kind
            units = doc.get("units", [])
            read = False
            body_names = []
            body_error = "尚无可读正文，请查看处理结果或重试未完成项。"
            for unit in units or [None]:
                if identity.kind == "legacy" and identity.value in source_ids:
                    body_error = "旧资料标识与新资料冲突，请先核对旧记录。"
                    continue
                try:
                    value = read_document_source(
                        kb_dir,
                        identity.value,
                        scope=scope,
                        unit_id=unit["unit_id"] if unit else None,
                    )
                    available = (
                        value
                        and value.get("content", "").strip()
                        and (
                            identity.kind == "legacy"
                            or value.get("knowledge_revision_id")
                            or value.get("units")
                            or value.get("unit_kind")
                        )
                    )
                    read = read or bool(available)
                    if available and value is not None and value.get("doc_name"):
                        body_names.append(value["doc_name"])
                except (ValueError, OSError):
                    body_error = "保存的正文缺失或已改变，请查看详情并重新处理。"
            names = [unit["doc_name"] for unit in units] or body_names
            ask = read and any(
                f"sources/{name}{ext}" in readable_files
                for name in names
                for ext in (".md", ".json")
            )
            if identity.kind == "source":
                ask = ask and any(
                    unit.get("successful_source_revision_id") in readable_revisions
                    for unit in units
                )
            mutable = not scope.historical and not doc.get("removed")
            unfinished = identity.kind == "source" and (
                not units
                or any(
                    unit.get("status")
                    not in {"completed", "empty", "retired", "awaiting_confirmation"}
                    for unit in units
                )
            )
            if doc.get("original_kind") == "legacy_snapshot":
                unfinished = False
            saved = read and (
                identity.kind == "legacy"
                or any(
                    unit.get("successful_revision_id")
                    and unit.get("successful_revision_id") == unit.get("target_revision_id")
                    for unit in units
                )
            )
            actions = {
                "read": Eligibility(read, "" if read else body_error),
                "ask": Eligibility(
                    ask,
                    ""
                    if ask
                    else "尚无当前问答依据；请从更多重试未完成项、预览重新处理或更新知识。",
                ),
                "retry": Eligibility(
                    mutable and unfinished,
                    ""
                    if unfinished
                    else "没有未完成项。"
                    if identity.kind == "source"
                    else "旧资料需先预览重新处理。",
                ),
                "recompile": Eligibility(
                    mutable and saved and not doc.get("contribution_empty"),
                    "" if saved else "缺少当前目标的已保存正文或索引；请先重试或重新处理。",
                ),
                "reprocess": Eligibility(mutable),
                "remove": Eligibility(mutable),
                "empty": Eligibility(
                    mutable and identity.kind == "source" and not doc.get("contribution_empty")
                ),
            }
            if identity.value in source_ids & legacy_ids:
                actions["recompile"] = Eligibility(
                    False, "旧资料标识与新资料冲突，请先核对旧记录。"
                )
                if identity.kind == "legacy":
                    for name in ("read", "ask", "reprocess", "remove"):
                        actions[name] = Eligibility(False, body_error)
            if not mutable:
                for name in ("retry", "recompile", "reprocess", "remove", "empty"):
                    actions[name] = Eligibility(
                        False,
                        "资料已撤下，保留历史读取。"
                        if doc.get("removed")
                        else "历史快照只读，请返回当前知识库操作。",
                    )
            status = _STATUS.get(doc.get("status"), "已导入（旧资料）")
            if doc.get("validity") == "needs_refresh" or any(
                unit.get("successful_source_revision_id")
                and unit.get("successful_source_revision_id")
                != unit.get("target_source_revision_id")
                for unit in units
            ):
                status += " · 知识待更新"
            elif doc.get("validity") in {"empty", "historical", "withdrawn"} and not doc.get(
                "removed"
            ):
                status += (
                    " · "
                    + {"empty": "贡献已清空", "historical": "历史依据", "withdrawn": "已撤下"}[
                        doc["validity"]
                    ]
                )
            details = [f"{doc['name']} · {status}", f"资料身份：{identity.kind} / {identity.value}"]
            if doc.get("location"):
                details.append(f"原输入位置：{doc['location']}")
            for key in (
                "source_revision_id",
                "length_class",
                "execution_mode",
                "pages",
                "characters",
                "tokens",
                "message",
            ):
                if doc.get(key) is not None:
                    details.append(f"{key}：{doc[key]}")
            details.extend(
                action.reason for action in actions.values() if not action.allowed and action.reason
            )
            rows.append(
                DocumentRow(identity, doc, status, actions, "\n".join(dict.fromkeys(details)))
            )
        return tuple(rows)
