"""Bind extracted references to their parent and import each child independently."""

from openkb.attachments import DocumentAttachment
from openkb.sources import SourceStore, content_id


def document_attachments(kb_dir, source, parsed):
    """Bind all child sources before model work or a parent publication can begin."""
    store = SourceStore(kb_dir)
    if parsed.input_key != source.input_key:
        raise ValueError("Attachment references do not belong to the parent version")
    children = {}
    for block in parsed.blocks:
        for item in block.location.get("attachment_files", []):
            if not item["parseable"]:
                continue
            child = store.intake_attachment(
                source,
                part=item["part"],
                name=item["name"],
                content=store.asset(item["blob"]).read_bytes(),
            )
            children[child.id] = DocumentAttachment(
                child.source_id, child.id, item["part"], item["name"]
            )
    return tuple(children.values())


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
