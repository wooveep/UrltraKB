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
    from pageindex import LocalClient
    from pageindex.storage.sqlite import SQLiteStorage

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
def pdf_model(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key")
    fixture = SimpleNamespace(last_page=3)

    def completion(**kwargs):
        prompt = str(kwargs["messages"])
        if "expert in extracting hierarchical tree structure" in prompt:
            response = [
                {
                    "structure": "1",
                    "title": "First section",
                    "physical_index": "<physical_index_1>",
                },
                {
                    "structure": "2",
                    "title": "Last section",
                    "physical_index": f"<physical_index_{fixture.last_page}>",
                },
            ]
        elif "toc_detected" in prompt:
            response = {"toc_detected": "no"}
        elif "start_begin" in prompt:
            response = {"start_begin": "yes"}
        elif '"answer"' in prompt:
            response = {"answer": "yes"}
        else:
            response = {
                "description": "Physical source fixture",
                "content": "First and last sections.",
                "create": [],
                "update": [],
                "related": [],
            }
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(response)), finish_reason="stop"
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    async def acompletion(**kwargs):
        return completion(**kwargs)

    monkeypatch.setattr("litellm.completion", completion)
    monkeypatch.setattr("litellm.acompletion", acompletion)
    return fixture


def test_pageindex_full_and_range_reads_recover_the_same_managed_pdf(
    tmp_path, physical_pdf, pdf_model
):
    original_store, moved_store = tmp_path / "original", tmp_path / "moved"
    with _client(model="gpt-4o", storage_path=str(original_store)) as client:
        identity = client.collection().add(str(physical_pdf))
    shutil.copytree(original_store, moved_store)
    shutil.rmtree(original_store)
    physical_pdf.unlink()
    with _client(model="gpt-4o", storage_path=str(moved_store)) as client:
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


def test_pdf_index_policy_changes_cannot_hit_an_older_tree(tmp_path, physical_pdf, pdf_model):
    from pageindex import IndexConfig

    storage = str(tmp_path / "index")
    with _client(model="gpt-4o", storage_path=storage) as client:
        first = client.collection().add(str(physical_pdf))
        assert client.collection().add(str(physical_pdf)) == first
    with _client(
        model="gpt-4o",
        storage_path=storage,
        index_config=IndexConfig(
            max_token_num_each_node=18000,
        ),
    ) as client:
        assert client.collection().add(str(physical_pdf)) != first


def test_legacy_cache_recovery_keeps_images_readable_without_claiming_old_policy(
    tmp_path, physical_pdf, pdf_model
):
    storage = tmp_path / "legacy-index"
    with _client(model="gpt-4o", storage_path=str(storage)) as client:
        identity = client.collection().add(str(physical_pdf))
        # Simulate the pre-metadata database and its missing page cache.
        with sqlite3.connect(storage / "pageindex.db") as db:
            db.execute("UPDATE documents SET pages=NULL, metadata=NULL")
        page = client.collection().get_page_content(identity, "3")[0]
        full = client.collection().get_document(identity, include_text=True)
    assert Path(page["images"][0]["path"]).is_file()
    assert page["images"][0]["path"] in json.dumps(full["structure"])
    assert full["metadata"]["coverage"] == "unknown"


def test_cache_recovery_rejects_a_missing_original_parser(tmp_path, physical_pdf, pdf_model):
    from pageindex.parser.pdf import PdfParser

    class MarkedPdfParser(PdfParser):
        def parse(self, *args, **kwargs):
            parsed = super().parse(*args, **kwargs)
            parsed.nodes[-1].content += "\nCustom extraction marker"
            return parsed

    storage = tmp_path / "custom-index"
    with _client(model="gpt-4o", storage_path=str(storage)) as client:
        client.register_parser(MarkedPdfParser())
        identity = client.collection().add(str(physical_pdf))
    with sqlite3.connect(storage / "pageindex.db") as db:
        db.execute("UPDATE documents SET pages=NULL")
    with _client(model="gpt-4o", storage_path=str(storage)) as client:
        with pytest.raises(ValueError, match="parser"):
            client.collection().get_page_content(identity, "3")


def test_managed_copy_preserves_existing_markdown_title_semantics(tmp_path, pdf_model):
    from pageindex import IndexConfig

    source = tmp_path / "human-readable.md"
    source.write_text("A document without headings.")
    with _client(
        storage_path=str(tmp_path / "index"),
        index_config=IndexConfig(if_add_node_summary=False, if_add_doc_description=False),
    ) as client:
        identity = client.collection().add(str(source))
        result = client.collection().get_document(identity)
    assert result["structure"][0]["title"] == "human-readable"


@pytest.mark.parametrize("damage", ["reference", "markdown_reference", "tail"])
def test_reading_rejects_a_replaced_or_truncated_page_map(kb_dir, physical_pdf, pdf_model, damage):
    from openkb.application.documents import import_document
    from openkb.documents import read_document_source
    from openkb.view_records import SourceMetadata

    imported = import_document(
        kb_dir,
        physical_pdf,
        metadata=SourceMetadata(product="Readback", applicable_versions=("1",), family="manual"),
    )
    assert imported.status == "added", imported.message
    unit = imported.units[0]
    directory = (
        kb_dir / ".openkb/knowledge" / unit.view_id / "revisions" / unit.knowledge_revision_id
    )
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    page_map = directory / "wiki/sources/manual.json"
    pages = json.loads(page_map.read_text())
    if damage in {"reference", "markdown_reference"}:
        pages[0]["content"] = "Other source body"
        reference = "sources/other.json" if damage == "reference" else "sources/other.md"
        (directory / "wiki" / reference).write_text(
            json.dumps(pages) if damage == "reference" else "Other source body"
        )
        manifest["source_map"]["path"] = reference
        manifest_path.write_text(json.dumps(manifest))
    else:
        page_map.write_text(json.dumps(pages[:-1]))
    with pytest.raises(ValueError, match="[Mm]ap"):
        read_document_source(kb_dir, imported.source_id)


@pytest.mark.parametrize("threshold", [0, 20])
def test_application_reads_physical_ranges_from_the_actual_historical_revision(
    kb_dir, physical_pdf, pdf_model, threshold, monkeypatch
):
    import yaml

    from openkb.application.documents import import_document
    from openkb.application.views import view_scope
    from openkb.documents import read_document_source
    from openkb.view_records import SourceMetadata

    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["pageindex_threshold"] = threshold
    config_path.write_text(yaml.safe_dump(config))
    imported = import_document(
        kb_dir,
        physical_pdf,
        metadata=SourceMetadata(
            product="Readback",
            applicable_versions=("1",),
            family="manual",
        ),
    )
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

    from openkb.cli import cli

    result = CliRunner().invoke(
        cli,
        [
            "--kb-dir",
            str(kb_dir),
            "--view",
            scope.view_id,
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

    from openkb import config as settings
    from openkb.api import create_app

    monkeypatch.setattr(settings, "GLOBAL_CONFIG_DIR", kb_dir / "settings")
    monkeypatch.setattr(settings, "GLOBAL_CONFIG_PATH", kb_dir / "settings/global.yaml")
    monkeypatch.setattr(settings, "GLOBAL_CONFIG_LOCK_PATH", kb_dir / "settings/.lock")
    settings.register_kb_alias("readback", kb_dir)
    monkeypatch.setenv("OPENKB_API_TOKEN", "fixture-token")
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


@pytest.mark.parametrize(
    "selection", [{"blocks": "999"}, {"chars": "0:3"}, {"pages": "1", "blocks": "1"}]
)
def test_legacy_pdf_without_a_source_map_rejects_character_and_block_selectors(
    kb_dir, physical_pdf, pdf_model, selection
):
    from openkb.application.documents import import_document
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.documents import read_document_source
    from openkb.source_pages import PageRangeError

    apply_kb_config_patch(
        kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"pdf_short_max_pages": 0})
    )
    result = import_document(kb_dir, physical_pdf)
    assert result.status == "added", result.message
    unit = result.units[0]
    path = (
        kb_dir
        / ".openkb/knowledge"
        / unit.view_id
        / "revisions"
        / unit.knowledge_revision_id
        / "manifest.json"
    )
    manifest = json.loads(path.read_text("utf-8"))
    manifest["source_map"] = None
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(PageRangeError):
        read_document_source(kb_dir, result.source_id, **selection)
