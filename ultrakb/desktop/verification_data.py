"""Real retained inputs for opt-in Qt acceptance, without invoking a model."""

import json
from uuid import uuid4

from ultrakb.application.views import published_scope
from ultrakb.locks import atomic_write_text
from ultrakb.processing_policy import classify_markdown_tokens
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.index_store import IndexStore
from ultrakb.storage.knowledge_store import KnowledgeStore
from ultrakb.storage.session_store import SessionStore
from ultrakb.storage.wiki_projection import ProjectionStore


def retain_document(kb, name, doc_name, text, *, pages=None, summary=None, assets=None):
    scope = published_scope(kb)
    bound = scope.knowledge
    catalog = CatalogStore(kb, bound.identity)
    original = kb / "raw" / name
    original.parent.mkdir(exist_ok=True)
    if pages is not None:
        import pymupdf

        with pymupdf.open() as document:
            for item in pages:
                document.new_page().insert_text((72, 72), item["content"])
            document.save(original)
    else:
        atomic_write_text(original, text)
    admitted = catalog.admit(original, source_key=str(original.resolve()))
    with bound.compile_workspace() as workspace:
        body = workspace.directory / "fixture-body.md"
        body.write_text(text)
        files = {"body.md": body}
        for path, data in (assets or {}).items():
            asset = workspace.directory / path
            asset.parent.mkdir(parents=True, exist_ok=True)
            asset.write_bytes(data)
            files[path] = asset
        if pages is not None:
            source = workspace.directory / "fixture-pages.json"
            source.write_text(json.dumps(pages, ensure_ascii=False))
            files["pages.json"] = source
        processing = catalog.retain_processing(
            admitted.revision_id,
            files,
            body_path="body.md",
            source_path="pages.json" if pages is not None else None,
            source_kind="pages" if pages is not None else "markdown",
            fingerprint="native-qt-fixture-v1",
            operation_id=uuid4().hex,
            decision=classify_markdown_tokens(50_000 if pages is not None else 100),
        )
    unit = catalog.plan_unit(processing.processing_id, key="main", doc_name=doc_name)
    retained_units = tuple(item for item in bound.input_units() if item.doc_name != doc_name)
    retained_ids = {item.unit_revision_id for item in retained_units}
    indexes = IndexStore(kb, bound.identity)
    index_revisions = [
        key
        for key in bound.index_revisions()
        if indexes.revision(key).unit_revision_id in retained_ids
    ]
    if pages is not None:
        index = indexes.accept(
            unit.unit_revision_id,
            {
                "doc_id": uuid4().hex,
                "doc_name": doc_name,
                "metadata": {
                    "unit_kind": "page",
                    "unit_count": len(pages),
                    "coverage": "complete",
                },
                "pages": pages,
                "structure": [
                    {
                        "node_id": "fixture-root",
                        "title": doc_name,
                        "summary": text,
                        "text": text,
                        "start_index": 1,
                        "end_index": len(pages),
                    }
                ],
            },
            content_fingerprint="a" * 64,
        )
        index_revisions.append(index.index_revision_id)
    changes = {
        f"sources/{doc_name}.json" if pages is not None else f"sources/{doc_name}.md": json.dumps(
            pages, ensure_ascii=False
        ).encode()
        if pages is not None
        else text.encode(),
    }
    if summary is not None:
        changes[f"summaries/{doc_name}.md"] = summary.encode()
    changes.update({f"sources/{path}": data for path, data in (assets or {}).items()})
    store = KnowledgeStore(kb, bound.identity)
    prepared = store._prepare_revision(
        changes,
        expected=bound.publication_id,
        view_id=bound.publication.view_id,
        unit_revisions=(*(item.unit_revision_id for item in retained_units), unit.unit_revision_id),
        index_revisions=tuple(index_revisions),
        generated_paths=(f"summaries/{doc_name}.md",) if summary is not None else (),
        base=bound,
    )
    publication = store.publish(prepared)
    ProjectionStore(kb, bound.identity).sync()
    return publication


def retain_conversation(kb, turns):
    bound = published_scope(kb).knowledge
    store, identity = SessionStore(kb, bound.identity), uuid4().hex
    store.create(identity, model="openai/verification", language="zh")
    for user, assistant in turns:
        store.append_exchange(identity, uuid4().hex, user, assistant)
    return identity, store


def publish_pages(kb, changes):
    """Create a competing committed page revision for confirmation checks."""
    bound = published_scope(kb).knowledge
    store = KnowledgeStore(kb, bound.identity)
    publication = store.publish(store.prepare_changes(bound, changes))
    ProjectionStore(kb, bound.identity).sync()
    return publication
