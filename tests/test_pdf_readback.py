"""Physical PDF coordinates survive blank pages, relocation and cache loss."""

import json
import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


@contextmanager
def _client(storage_path, **kwargs):
    from pageindex import IndexConfig, LocalClient
    from pageindex.storage.sqlite import SQLiteStorage

    kwargs.setdefault("model", "deepseek/deepseek-flash")
    kwargs.setdefault(
        "index_config", IndexConfig(if_add_node_summary=False, if_add_doc_description=False)
    )
    if runtime := kwargs.pop("llm_client", None):
        kwargs["index_config"] = kwargs["index_config"].model_copy(
            update={"llm_client": runtime, "require_llm_client": True}
        )
    with SQLiteStorage(str(Path(storage_path) / "pageindex.db")) as storage:
        yield LocalClient(storage_path=storage_path, storage=storage, **kwargs)


@pytest.fixture
def physical_pdf(tmp_path):
    import pymupdf

    target = tmp_path / "manual.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "First section\nPhysical page one.")
        pdf.new_page()
        last = pdf.new_page()
        last.insert_text((72, 72), "Last section\nPhysical page three.")
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 64, 64), False)
        pixmap.clear_with(128)
        last.insert_image(pymupdf.Rect(72, 120, 136, 184), stream=pixmap.tobytes("png"))
        pdf.set_page_labels([{"startpage": 0, "style": "r", "firstpagenum": 1}])
        pdf.set_metadata({"title": "Readback V1 Manual"})
        pdf.save(target)
    return target


@pytest.fixture
def pdf_model(request, monkeypatch, live_llm):
    from application_llm_fixtures import configure_application_model

    live_llm.install(monkeypatch)
    # Some CLI cases construct their own KB in tmp_path during the test. Do
    # not create a second KB before those cases have exercised initialization.
    if "kb_dir" in request.fixturenames:
        configure_application_model(request.getfixturevalue("kb_dir"))
    fixture = SimpleNamespace(last_page=3)
    return fixture


@pytest.fixture
def pdf_index_model(monkeypatch, live_llm):
    from test_index_llm import RecordingIndexLLM

    live_llm.install(monkeypatch)
    return RecordingIndexLLM(live_llm=live_llm)


@pytest.fixture
def published_pdf(kb_dir, physical_pdf, live_llm):
    from publication_fixtures import publish_document

    return publish_document(
        kb_dir,
        physical_pdf,
        "# Manual\n\nPhysical page one.\n\nPhysical page three.",
        bundle=live_llm.credentials,
    )


def test_pageindex_full_and_range_reads_recover_the_same_managed_pdf(
    tmp_path, physical_pdf, pdf_index_model
):
    original_store, moved_store = tmp_path / "original", tmp_path / "moved"
    with _client(storage_path=str(original_store), llm_client=pdf_index_model) as client:
        identity = client.collection().add(str(physical_pdf))
    indexed_calls = len(pdf_index_model.calls)
    shutil.copytree(original_store, moved_store)
    shutil.rmtree(original_store)
    physical_pdf.unlink()
    with _client(storage_path=str(moved_store), llm_client=pdf_index_model) as client:
        collection = client.collection()
        cached = collection.get_page_content(identity, "1-3")
        full = collection.get_document(identity, include_text=True)
        assert [page["page"] for page in cached] == [1, 2, 3]
        assert cached[1]["content"] == ""
        assert cached[2]["printed_page_label"] == "iii"
        assert Path(cached[2]["images"][0]["path"]).is_file()
        with sqlite3.connect(moved_store / "pageindex.db") as db:
            db.execute("UPDATE documents SET pages = NULL WHERE doc_id = ?", (identity,))
        assert collection.get_page_content(identity, "1-3") == cached
        assert collection.get_document(identity, include_text=True) == full
        assert full["metadata"]["coverage"] == "complete"
        assert full["metadata"]["processing_fingerprint"]
    assert len(pdf_index_model.calls) == indexed_calls


def test_pdf_index_policy_changes_cannot_hit_an_older_tree(tmp_path, physical_pdf, pdf_index_model):
    from pageindex import IndexConfig

    storage = str(tmp_path / "index")
    with _client(storage_path=storage, llm_client=pdf_index_model) as client:
        first = client.collection().add(str(physical_pdf))
        assert client.collection().add(str(physical_pdf)) == first
    with _client(
        storage_path=storage,
        llm_client=pdf_index_model,
        index_config=IndexConfig(
            max_token_num_each_node=18000,
            if_add_node_summary=False,
            if_add_doc_description=False,
        ),
    ) as client:
        assert client.collection().add(str(physical_pdf)) != first


def test_legacy_cache_recovery_keeps_images_readable_without_claiming_old_policy(
    tmp_path, physical_pdf, pdf_index_model
):
    storage = tmp_path / "legacy-index"
    with _client(storage_path=str(storage), llm_client=pdf_index_model) as client:
        identity = client.collection().add(str(physical_pdf))
        indexed_calls = len(pdf_index_model.calls)
        # Simulate the pre-metadata database and its missing page cache.
        with sqlite3.connect(storage / "pageindex.db") as db:
            db.execute("UPDATE documents SET pages=NULL, metadata=NULL")
        page = client.collection().get_page_content(identity, "3")[0]
        full = client.collection().get_document(identity, include_text=True)
    assert Path(page["images"][0]["path"]).is_file()
    assert page["images"][0]["path"] in json.dumps(full["structure"])
    assert full["metadata"]["coverage"] == "unknown"
    assert len(pdf_index_model.calls) == indexed_calls


def test_cache_recovery_rejects_a_missing_original_parser(tmp_path, physical_pdf, pdf_index_model):
    from pageindex.parser.pdf import PdfParser

    class MarkedPdfParser(PdfParser):
        def parse(self, *args, **kwargs):
            parsed = super().parse(*args, **kwargs)
            parsed.nodes[-1].content += "\nCustom extraction marker"
            return parsed

    storage = tmp_path / "custom-index"
    with _client(storage_path=str(storage), llm_client=pdf_index_model) as client:
        client.register_parser(MarkedPdfParser())
        identity = client.collection().add(str(physical_pdf))
    with sqlite3.connect(storage / "pageindex.db") as db:
        db.execute("UPDATE documents SET pages=NULL")
    indexed_calls = len(pdf_index_model.calls)
    with _client(storage_path=str(storage), llm_client=pdf_index_model) as client:
        with pytest.raises(ValueError, match="parser"):
            client.collection().get_page_content(identity, "3")
    assert len(pdf_index_model.calls) == indexed_calls


def test_managed_copy_preserves_existing_markdown_title_semantics(tmp_path):
    from pageindex import IndexConfig
    from test_index_llm import RecordingIndexLLM

    source = tmp_path / "human-readable.md"
    source.write_text("A document without headings.")
    with _client(
        storage_path=str(tmp_path / "index"),
        index_config=IndexConfig(if_add_node_summary=False, if_add_doc_description=False),
        llm_client=RecordingIndexLLM(),
    ) as client:
        identity = client.collection().add(str(source))
        result = client.collection().get_document(identity)
    assert result["structure"][0]["title"] == "human-readable"


@pytest.mark.parametrize("threshold", [0, 20])
def test_application_reads_physical_ranges_from_the_actual_historical_revision(
    kb_dir, physical_pdf, published_pdf, threshold, monkeypatch
):
    import yaml

    from ultrakb.application.views import view_scope
    from ultrakb.documents import read_document_source
    from ultrakb.locks import atomic_write_text, kb_ingest_lock

    config_path = kb_dir / ".ultrakb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["pageindex_threshold"] = threshold
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        atomic_write_text(config_path, yaml.safe_dump(config))
    imported = published_pdf
    assert imported.status == "added", imported.message
    scope = view_scope(
        kb_dir,
        imported.units[0].view_id,
        historical_revision=imported.units[0].knowledge_revision_id,
    )
    physical_pdf.unlink()
    whole = read_document_source(kb_dir, imported.source_id, scope=scope)
    blank = read_document_source(kb_dir, imported.source_id, scope=scope, pages="2")
    last = read_document_source(kb_dir, imported.source_id, scope=scope, pages="3")
    assert whole["pages"] == 3 and whole["coverage"] == "complete"
    assert blank["page_range"] == [2] and "[Physical page 2]" in blank["content"]
    assert "Physical page three." in last["content"] and "[Physical page 3]" in last["content"]
    assert last["source_revision_id"] == imported.source_revision_id
    assert last["validity"] == "historical"

    from click.testing import CliRunner

    from ultrakb.cli import cli

    result = CliRunner().invoke(
        cli,
        [
            "--kb-dir",
            str(kb_dir),
            "source",
            imported.source_id,
            "--pages",
            "3",
            "--knowledge-revision",
            imported.units[0].knowledge_revision_id,
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["content"] == last["content"]

    from fastapi.testclient import TestClient

    from ultrakb import config as settings
    from ultrakb.api import create_app

    monkeypatch.setattr(settings, "GLOBAL_CONFIG_DIR", kb_dir / "settings")
    monkeypatch.setattr(settings, "GLOBAL_CONFIG_PATH", kb_dir / "settings/global.yaml")
    monkeypatch.setattr(settings, "GLOBAL_CONFIG_LOCK_PATH", kb_dir / "settings/.lock")
    settings.register_kb_alias("readback", kb_dir)
    monkeypatch.setenv("ULTRAKB_API_TOKEN", "fixture-token")
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/document/source",
            headers={"Authorization": "Bearer fixture-token"},
            json={
                "kb": "readback",
                "hash": imported.source_id,
                "pages": "3",
                "view_id": scope.view_id,
                "knowledge_revision_id": imported.units[0].knowledge_revision_id,
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["content"] == last["content"]
