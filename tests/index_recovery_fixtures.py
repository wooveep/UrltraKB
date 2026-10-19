"""Real SDK index acceptance followed by an injected SQLite compiler-write failure."""

from types import SimpleNamespace

import pymupdf
import pytest
from test_storage_conversation_writes import StreamTransport
from test_storage_indexing import indexing_response

from ultrakb.application.documents import import_document
from ultrakb.application.execution import ExecutionContext
from ultrakb.application.knowledge_bases import initialize_kb
from ultrakb.application.views import published_scope
from ultrakb.config import save_config
from ultrakb.desktop.verification_data import publish_pages
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.index_store import IndexStore

TRIGGER = """CREATE TRIGGER interrupt_summary_generation BEFORE INSERT ON ukb_compile_invocations
WHEN NEW.stage = 'summary' OR NEW.stage LIKE 'document-summary:%'
BEGIN SELECT RAISE(ABORT, 'compiler checkpoint write interrupted after indexing'); END"""


def index_ids(root, identity, unit_revision_id):
    with storage_stage(root, identity, read_only=True) as db:
        return tuple(
            row[0]
            for row in db.connection.execute(
                "SELECT index_revision_id FROM ukb_index_revisions WHERE unit_revision_id = ?",
                (unit_revision_id,),
            )
        )


@pytest.fixture
def retained_index(tmp_path):
    root, source = tmp_path / "kb", tmp_path / "manual.pdf"
    initialize_kb(root, seed_environment=False)
    save_config(
        root / ".ultrakb/config.yaml",
        {
            "model": "openai/gpt-4o-mini",
            "language": "en",
            "pdf_short_max_pages": 0,
            "model_capacity": {"max_input_tokens": 32768, "tokenizer_model": "gpt-4o-mini"},
            "model_policy": {"max_calls": 16, "retries": 0},
        },
    )
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "The value is 17.")
        pdf.save(source)
    publish_pages(
        root, {"concepts/operator-note.md": b"# Human note\nKeep the reviewed procedure.\n"}
    )
    scope = published_scope(root)
    with storage_stage(root, scope.knowledge.identity) as db:
        db.connection.execute(TRIGGER)
    context = ExecutionContext()
    with StreamTransport(indexing_response) as service:
        (root / ".env").write_text(
            f"LLM_API_KEY=offline-index-key\nOPENAI_API_BASE={service.url}\n"
        )
        failed = import_document(root, source, context=context)
        assert failed.status in {"failed", "blocked"}, failed.message
        [unit] = failed.units
        catalog = CatalogStore(root, scope.knowledge.identity)
        target = catalog.unit(unit.unit_id)
        [index] = index_ids(root, scope.knowledge.identity, target.unit_revision_id)
        document = IndexStore(root, scope.knowledge.identity).document(index)
        assert "17" in str(document) and service.requests
        source.unlink()
        yield SimpleNamespace(
            root=root,
            identity=scope.knowledge.identity,
            target=target,
            index=index,
            document=document,
            service=service,
            context=context,
            original=catalog.read_original(failed.source_revision_id),
            publication_id=scope.publication_id,
            sent=len(service.requests),
        )
