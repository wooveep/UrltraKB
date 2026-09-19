"""Explicit processing of previously registered attachment sources."""

from openkb.sources import SourceStore, content_id


def import_attachment(kb_dir, request, *, context):
    from openkb.application.document_pipeline import compile_version
    from openkb.application.documents import DocumentResult
    from openkb.config import resolve_effective_config
    from openkb.locks import kb_ingest_lock

    with kb_ingest_lock(kb_dir / ".openkb", cancelled=context.cancelled, on_wait=context.waiting):
        store = SourceStore(kb_dir)
        parent = store.version(request.parent_version_id)
        child = store.current(request.source_id)
        if (
            child.id != request.version_id
            or child.origin != f"attachment:{parent.source_id}/{content_id(request.part)}"
        ):
            raise ValueError("The retained document attachment changed; review its latest source")
        if store.current(parent.source_id).id != parent.id:
            return DocumentResult(
                child.origin,
                "skipped",
                (str(store.original(child)),),
                source_intake="saved",
                source_id=child.source_id,
                input_version=child.id,
                reason="attachment_parent_version_changed",
            )
        with context.begin(kb_dir) as credentials:
            return compile_version(
                kb_dir,
                child,
                resolve_effective_config(kb_dir)[0],
                bundle=credentials,
                on_event=context.on_event,
            )
