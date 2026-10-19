"""Real retained input whose local preparation stopped before any model execution."""

from ultrakb.application.views import published_scope
from ultrakb.compile_checkpoints import digest
from ultrakb.converter import _sanitize_stem
from ultrakb.inputs import prepared_input
from ultrakb.locks import atomic_write_text
from ultrakb.processing_policy import classify_markdown_tokens
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.compile_workspace import materialize_baseline
from ultrakb.storage.pending_store import PendingStore
from ultrakb.text_formats import normalize_text_document


def retain_failure(kb, path):
    bound = published_scope(kb).knowledge
    catalog = CatalogStore(kb, bound.identity)
    with prepared_input(path) as ready, materialize_baseline(bound, ()) as baseline:
        revision = catalog.admit(ready.path, source_key=str(ready.identity), label=path.name)
        name = f"{_sanitize_stem(path.stem)}-{revision.source_id[:8]}"
        frozen = normalize_text_document(ready, name, baseline.directory)
        body, native = baseline.directory / "body.md", baseline.directory / "body.content.json"
        atomic_write_text(body, frozen.text)
        atomic_write_text(native, frozen.model_dump_json())
        processing = catalog.retain_processing(
            revision.revision_id,
            {"body.md": body, "body.content.json": native},
            body_path="body.md",
            source_path="body.content.json",
            source_kind="text",
            fingerprint=digest(frozen.model_dump(mode="json")),
            operation_id=digest([revision.revision_id, "local-preparation-fixture"]),
            decision=classify_markdown_tokens(frozen.tokens).model_copy(
                update={"characters": len(frozen.text)}
            ),
        )
        target = catalog.plan_unit(processing.processing_id, key="body", doc_name=name)
    pending = PendingStore(kb, bound.identity)
    queued = pending.enqueue(revision.revision_id, unit_revision_id=target.unit_revision_id)
    started = pending.start(pending.claim(intent_id=queued.intent_id))
    pending.finish(started, status="failed", error_code="LocalPreparationInterrupted")
    return catalog.source(revision.source_id)
