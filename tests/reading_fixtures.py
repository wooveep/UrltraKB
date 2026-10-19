"""Publish explicitly declared physical-page evidence in the native catalog."""

from ultrakb.desktop.verification_data import retain_document


def publish_original(kb, records, *, doc_name="manual"):
    assets = {
        image["path"].removeprefix("sources/"): (kb / "wiki" / image["path"]).read_bytes()
        for record in records
        for image in record.get("images", [])
    }
    return retain_document(
        kb,
        f"{doc_name}.pdf",
        doc_name,
        "\n\n".join(record["content"] for record in records),
        pages=records,
        assets=assets,
    )


def publish_text_original(kb, original, body, mapping, *, doc_name="manual"):
    from uuid import uuid4

    from ultrakb.application.views import published_scope
    from ultrakb.processing_policy import classify_markdown_tokens
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.knowledge_store import KnowledgeStore
    from ultrakb.storage.wiki_projection import ProjectionStore

    bound = published_scope(kb).knowledge
    catalog = CatalogStore(kb, bound.identity)
    admitted = catalog.admit(original, source_key=str(original.resolve()))
    processing = catalog.retain_processing(
        admitted.revision_id,
        {"body.md": body, "body.content.json": mapping},
        body_path="body.md",
        source_path="body.content.json",
        source_kind="text",
        fingerprint="explicit-reader-fixture-v1",
        operation_id=uuid4().hex,
        decision=classify_markdown_tokens(1),
    )
    unit = catalog.plan_unit(processing.processing_id, key="body", doc_name=doc_name)
    store = KnowledgeStore(kb, bound.identity)
    units = tuple(u for u in bound.input_units() if u.doc_name != doc_name)
    store.publish(
        store._prepare_revision(
            {
                f"sources/{doc_name}.md": body.read_bytes(),
                f"sources/{doc_name}.content.json": mapping.read_bytes(),
            },
            expected=bound.publication_id,
            view_id=bound.publication.view_id,
            unit_revisions=(*(u.unit_revision_id for u in units), unit.unit_revision_id),
            index_revisions=bound.index_revisions(),
            base=bound,
        )
    )
    ProjectionStore(kb, bound.identity).sync()
