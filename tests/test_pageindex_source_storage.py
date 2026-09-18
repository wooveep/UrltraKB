"""Native imports remain usable through PageIndex's local database API."""

import asyncio
import json
import subprocess
import sys
from contextlib import contextmanager

import pytest
from pageindex import IndexConfig, LocalClient
from pageindex.storage.sqlite import SQLiteStorage

from openkb.application.conversations import ask_question
from openkb.application.documents import import_document
from openkb.application.source_actions import rebuild_source_navigation
from openkb.application.source_history import source_status
from tests.http_model_fixture import evidence_response


@contextmanager
def collection(kb_dir):
    with SQLiteStorage(str(kb_dir / ".openkb/pageindex.db")) as storage:
        client = LocalClient(
            model="openai/offline-test",
            storage_path=str(kb_dir / ".openkb"),
            storage=storage,
            index_config=IndexConfig(llm_params={"api_key": "offline-test"}),
        )
        yield client.collection()


def test_import_saves_original_pageindex_database_before_fact_analysis(
    kb_dir, tmp_path, model_service
):
    source = tmp_path / "handbook.md"
    source.write_text("# Backup\n\nVerify backup first.\n\n## Pressure\n\nKeep pressure at 37 kPa.")
    before_facts = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "facts":
            assert (kb_dir / ".openkb/pageindex.db").is_file()
            assert not list((kb_dir / ".openkb/source-store/navigation").glob("*.json"))
            assert not (kb_dir / ".openkb/source-store/navigation/prepared").exists()
            with collection(kb_dir) as documents:
                stored = documents.list_documents()
                assert len(stored) == 1
                doc_id = stored[0]["doc_id"]
                assert documents.get_document_structure(doc_id)
                pages = documents.get_page_content(doc_id, "1-4")
                before_facts.append([page["content"] for page in pages])
        return evidence_response(payload)

    model_service.respond = respond
    imported = import_document(kb_dir, source)
    assert imported.knowledge_compilation == "completed", imported
    assert before_facts
    assert all(
        rows == ["# Backup", "Verify backup first.", "## Pressure", "Keep pressure at 37 kPa."]
        for rows in before_facts
    )


@pytest.mark.parametrize(
    "damage", ["structure", "pages", "missing", "pages_shape", "metadata_json", "metadata_null"]
)
def test_question_cannot_silently_bypass_a_damaged_pageindex_database(
    kb_dir, tmp_path, model_service, damage
):
    source = tmp_path / "handbook.md"
    source.write_text("Pressure must be 37 kPa.")
    imported = import_document(kb_dir, source)
    assert imported.knowledge_compilation == "completed", imported
    nav = source_status(kb_dir, imported.source_id)["navigation"]
    import sqlite3
    from contextlib import closing

    doc_id = nav["pageindex"]["doc_id"]
    with closing(sqlite3.connect(kb_dir / ".openkb/pageindex.db")) as connection, connection:
        if damage == "missing":
            connection.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
        elif damage.startswith("metadata"):
            connection.execute(
                "UPDATE openkb_source_indexes SET metadata = ? WHERE doc_id = ?",
                ("{" if damage == "metadata_json" else "null", doc_id),
            )
        else:
            column = "structure" if damage == "structure" else "pages"
            value = json.loads(
                connection.execute(
                    f"SELECT {column} FROM documents WHERE doc_id = ?", (doc_id,)
                ).fetchone()[0]
            )
            if damage == "structure":
                value[0]["title"] = "Unrelated source"
            elif damage == "pages":
                value[0]["content"] = "Pressure must be 99 kPa."
            else:
                value = [1]
            connection.execute(
                f"UPDATE documents SET {column} = ? WHERE doc_id = ?", (json.dumps(value), doc_id)
            )
    requests = len(model_service)
    with pytest.raises(ValueError, match="PageIndex"):
        asyncio.run(ask_question(kb_dir, "What is the pressure?"))
    assert len(model_service) == requests
    repaired = rebuild_source_navigation(
        kb_dir, imported.source_id, version_id=imported.input_version, parse_id=imported.parse_id
    )
    assert repaired["pageindex"]["doc_id"] != doc_id
    with collection(kb_dir) as documents:
        text = documents.get_page_content(repaired["pageindex"]["doc_id"], "1")
        assert text[0]["content"] == "Pressure must be 37 kPa."
    repeated = rebuild_source_navigation(
        kb_dir, imported.source_id, version_id=imported.input_version, parse_id=imported.parse_id
    )
    assert repeated["id"] == repaired["id"]
    assert len(model_service) == requests
    from openkb.application.source_cleanup import cleanup_history, preview_history_cleanup

    preview_history_cleanup(kb_dir)  # Retired damage cannot block the whole KB's preview.
    source.write_text("Current pressure must be 22 kPa.")
    assert import_document(kb_dir, source).knowledge_compilation == "completed"
    cleanup_history(kb_dir, preview_history_cleanup(kb_dir).id)
    with collection(kb_dir) as documents:
        remaining = {row["doc_id"] for row in documents.list_documents()}
        assert doc_id not in remaining
        assert repaired["pageindex"]["doc_id"] not in remaining
    with closing(sqlite3.connect(kb_dir / ".openkb/pageindex.db")) as connection:
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        assert connection.execute("SELECT COUNT(*) FROM openkb_source_indexes").fetchone()[0] == 1
    assert not (kb_dir / ".openkb/files/default" / f"{doc_id}.openkb-index").exists()


def test_crashed_index_add_recovers_old_database_and_removes_managed_partial_input(
    kb_dir, tmp_path, model_service
):
    from openkb.locks import kb_ingest_lock

    source = tmp_path / "retained.md"
    source.write_text("Retain this published document.")
    assert import_document(kb_dir, source).knowledge_compilation == "completed"
    with collection(kb_dir) as documents:
        before = documents.list_documents()
    managed = kb_dir / ".openkb/files/default"
    before_files = {path.name: path.read_bytes() for path in managed.iterdir() if path.is_file()}
    pending = tmp_path / "interrupted.md"
    pending.write_text("A source interrupted during database indexing.")
    script = """
import os, sys
from pathlib import Path
from pageindex import LocalClient
from openkb.application.documents import import_document
register = LocalClient.register_parser
def register_crashing_parser(client, parser):
    class CrashingParser:
        def supported_extensions(self):
            return parser.supported_extensions()
        def parse(self, *args, **kwargs):
            os._exit(23)
    register(client, CrashingParser())
LocalClient.register_parser = register_crashing_parser
import_document(Path(sys.argv[1]), Path(sys.argv[2]))
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(kb_dir), str(pending)],
        capture_output=True,
        timeout=30,
    )
    assert child.returncode == 23, child.stderr.decode()
    with kb_ingest_lock(kb_dir / ".openkb"):
        with collection(kb_dir) as documents:
            assert documents.list_documents() == before
    assert {
        path.name: path.read_bytes() for path in managed.iterdir() if path.is_file()
    } == before_files


def test_large_native_source_reaches_analysis_through_bounded_sdk_reads(
    kb_dir, tmp_path, model_service
):
    from openkb.application.execution import ExecutionContext

    source = tmp_path / "large.md"
    source.write_text("\n\n".join(f"Requirement {number}." for number in range(1005)))
    stopped = False

    def respond(body):
        nonlocal stopped
        payload = json.loads(body["messages"][-1]["content"])
        assert payload["stage"] == "facts"
        stopped = True
        return evidence_response(payload)

    model_service.respond = respond
    result = import_document(kb_dir, source, context=ExecutionContext(cancelled=lambda: stopped))
    assert result.knowledge_compilation == "stopped", result
    assert stopped
    with collection(kb_dir) as documents:
        doc_id = documents.list_documents()[0]["doc_id"]
        assert documents.get_page_content(doc_id, "1001-1005")[-1]["content"] == "Requirement 1004."
