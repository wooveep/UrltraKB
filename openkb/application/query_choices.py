"""Human-readable, explicit choices for an unresolved question scope."""

from openkb.locks import kb_read_lock
from openkb.source_catalog import list_sources, read_record
from openkb.view_records import VersionAnnotation


def scope_candidates(kb_dir, views, *, product_ids=()):
    with kb_read_lock(kb_dir / ".openkb"):
        sources: dict[str, list[str]] = {}
        for source in list_sources(kb_dir):
            if source.removed or not source.annotation_id:
                continue
            annotation = read_record(kb_dir, "annotations", source.annotation_id, VersionAnnotation)
            sources.setdefault(annotation.view_id, []).append(source.name)
        return tuple(
            {
                "view_id": view.view_id,
                "product_id": view.product_id,
                "product": view.product or "产品待确认",
                "versions": list(view.applicable_versions),
                "sources": sorted(sources[view.view_id]),
                "action": "select_for_question",
                "reason": "请选择本次要使用的资料范围；此操作不确认别名或适用版本。",
            }
            for view in views
            if view.view_id in sources and (not product_ids or view.product_id in product_ids)
        )


def source_query_scope(kb_dir, source_name):
    """Select one current source by its exact filename; ambiguity is never guessed."""
    from openkb.application.views import list_views, view_scope

    choices = scope_candidates(kb_dir, list_views(kb_dir))
    matches = [item for item in choices if source_name in item["sources"]]
    if len(matches) != 1:
        raise ValueError("资料名称不唯一或不存在，请从候选列表选择完整资料名称。")
    return view_scope(kb_dir, matches[0]["view_id"])
