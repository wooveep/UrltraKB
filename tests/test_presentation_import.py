"""Hidden slides and speaker notes retain physical page identity at public seams."""

import shutil
from pathlib import Path

import pytest

pytest_plugins = ("test_office_import",)

FIXTURE = Path(__file__).parent / "fixtures/office/slides.pptx"


@pytest.fixture
def presentation_model(pdf_model, monkeypatch):
    import json
    from types import SimpleNamespace

    import litellm

    original = litellm.completion
    prompts = []

    def completion(**kwargs):
        prompt = str(kwargs["messages"])
        prompts.append(prompt)
        if "PHYSICAL SLIDE STRUCTURE" not in prompt:
            return original(**kwargs)
        result = original(**kwargs)
        result.choices[0].message = SimpleNamespace(
            content=json.dumps(
                [
                    {
                        "structure": "1",
                        "title": "Key rotation guidance",
                        "title_origin": "generated",
                        "physical_index": 1,
                        "anchor": {
                            "unit": 1,
                            "part": "notes",
                            "range": [0, 17],
                            "excerpt": "Speaker note only",
                        },
                    }
                ]
            )
        )
        text = "\n".join(message["content"] for message in kwargs["messages"])
        marker = '{"unit": 2, "parts":'
        if marker in text:
            unit, _ = json.JSONDecoder().raw_decode(text[text.index(marker) :])
            body = unit["parts"]["body"]
            start = body.index("AFTER_IMAGE_MARKER")
            entries = json.loads(result.choices[0].message.content)
            entries.append(
                {
                    "structure": "2",
                    "title": "Illustrated details",
                    "title_origin": "generated",
                    "physical_index": 2,
                    "anchor": {
                        "unit": 2,
                        "part": "body",
                        "range": [start, start + 18],
                        "excerpt": "AFTER_IMAGE_MARKER",
                    },
                }
            )
            result.choices[0].message.content = json.dumps(entries)
        return result

    async def acompletion(**kwargs):
        return completion(**kwargs)

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(litellm, "acompletion", acompletion)
    return prompts


@pytest.mark.parametrize("limit", [10, 1])
def test_pptx_retains_hidden_slides_and_notes_without_extra_pages(
    kb_dir, tmp_path, office_runtime, presentation_model, limit
):
    from openkb.application.documents import import_document
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.documents import read_document_source

    apply_kb_config_patch(
        kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"pdf_short_max_pages": limit})
    )
    path = tmp_path / "演示文稿.pptx"
    shutil.copyfile(FIXTURE, path)
    imported = import_document(kb_dir, path)
    assert imported.status == "added", imported.message
    source = read_document_source(kb_dir, imported.source_id)
    assert source["pages"] == 3
    assert "HIDDEN_SLIDE_BODY" in source["content"]
    assert "演讲备注" in source["content"]
    assert "rotate key every 90 days" in source["content"]
    assert [s["hidden"] for s in source["office"]["slides"]] == [False, True, False]
    assert "Alpha visible slide" in source["units"][0]["parts"]["body"]
    assert "HIDDEN_SLIDE_BODY" in source["units"][1]["parts"]["body"]
    page = read_document_source(kb_dir, imported.source_id, pages="3")
    assert page["units"][0]["parts"]["body"].strip() == ""
    assert page["units"][0]["parts"]["notes"] == "Notes-only page evidence."
    assert (kb_dir / source["original_path"]).read_bytes() == path.read_bytes()
    notes = read_document_source(kb_dir, imported.source_id, pages="1", part="notes")
    assert "rotate key every 90 days" in notes["content"]
    assert "Alpha visible slide" not in notes["content"]
    assert notes["origin_locators"][0]["part"] == "notes"
    if limit == 1:
        assert any("PHYSICAL SLIDE STRUCTURE" in prompt for prompt in presentation_model)


def test_notes_only_change_rebuilds_index_and_cache_recovers_both_parts(
    kb_dir, tmp_path, office_runtime, presentation_model
):
    import json
    import sqlite3

    from pageindex import IndexConfig
    from pageindex.storage.sqlite import SQLiteStorage

    from openkb.application.documents import import_document
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.block_package import create_index_client
    from openkb.documents import read_document_source

    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir),
            config={
                "model": "gpt-4o",
                "model_capacity": {"max_input_tokens": 1},
            },
        ),
    )
    source = tmp_path / "portable.pptx"
    shutil.copyfile(FIXTURE, source)
    imported = import_document(kb_dir, source)
    assert imported.status == "added", imported.message
    saved = read_document_source(kb_dir, imported.source_id)
    assert saved["length_class"] == "short" and saved["execution_mode"] == "segmented"
    retained = (kb_dir / saved["internal_pdf_path"]).with_suffix(".okpi")
    package = tmp_path / "slides.okpi"
    shutil.copyfile(retained, package)
    store = tmp_path / "standalone-index"
    with SQLiteStorage(str(store / "pageindex.db")) as database:
        client = create_index_client(
            storage_path=str(store),
            storage=database,
            model="gpt-4o",
            index_config=IndexConfig(if_add_node_summary=True),
        )
        collection = client.collection()
        first = collection.add(str(package))
        before = collection.get_document(first, include_text=True)
        assert before["page_count"] == 3
        assert before["structure"][0]["title_origin"] == "generated"
        assert before["structure"][0]["anchor"]["part"] == "notes"
        assert "rotate key every 90 days" in before["structure"][0]["text"]
        anchor = before["structure"][1]["anchor"]
        start, end = anchor["range"]
        assert saved["units"][1]["parts"]["body"][start:end] == "AFTER_IMAGE_MARKER"
        content = json.loads(package.read_text())
        content["slides"][2]["notes"] = "Changed notes, identical PDF bytes."
        package.write_text(json.dumps(content))
        second = collection.add(str(package))
        assert second != first
    moved = tmp_path / "moved-index"
    shutil.copytree(store, moved)
    shutil.rmtree(store)
    source.unlink()
    package.unlink()
    with SQLiteStorage(str(moved / "pageindex.db")) as database:
        collection = create_index_client(
            storage_path=str(moved), storage=database, model="gpt-4o"
        ).collection()
        expected = collection.get_page_content(first, "1-3")
        assert Path(expected[1]["images"][0]["path"]).is_file()
        before = collection.get_document(first, include_text=True)
        with sqlite3.connect(moved / "pageindex.db") as db:
            db.execute("UPDATE documents SET pages=NULL")
        calls = len(presentation_model)
        assert collection.get_page_content(first, "1-3") == expected
        assert collection.get_document(first, include_text=True) == before
        assert (
            collection.get_page_content(second, "3")[0]["parts"]["notes"]
            == "Changed notes, identical PDF bytes."
        )
        assert len(presentation_model) == calls


def test_presentation_api_and_cli_select_the_same_speaker_notes(
    kb_dir, office_runtime, presentation_model
):
    import json

    from click.testing import CliRunner
    from fastapi.testclient import TestClient

    from openkb.api import create_app
    from openkb.cli import cli
    from openkb.config import register_kb_alias

    register_kb_alias("presentation", kb_dir)
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/add",
            data={"kb": "presentation", "stream": "false"},
            files={"files": ("slides.pptx", FIXTURE.read_bytes())},
        )
        assert response.status_code == 200, response.text
        result = response.json()["files"][0]
        assert result["status"] == "added", result
        read = client.post(
            "/api/v1/document/source",
            json={
                "kb": "presentation",
                "hash": result["source_id"],
                "pages": "2",
                "part": "notes",
            },
        )
    assert read.status_code == 200, read.text
    notes = read.json()
    assert notes["part"] == "notes" and notes["pages"] == 3
    assert "Hidden speaker note" in notes["content"] and "HIDDEN_SLIDE_BODY" not in notes["content"]
    output = CliRunner().invoke(
        cli,
        ["--kb-dir", str(kb_dir), "source", result["source_id"], "--pages", "2", "--part", "notes"],
    )
    assert output.exit_code == 0, output.output
    assert json.loads(output.output)["content"] == notes["content"]


def test_note_anchor_cannot_claim_body_coordinates(
    kb_dir, office_runtime, presentation_model, monkeypatch
):
    import json

    import litellm

    from openkb.application.documents import import_document
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.documents import read_document_source

    original = litellm.completion

    def completion(**kwargs):
        result = original(**kwargs)
        if "PHYSICAL SLIDE STRUCTURE" in str(kwargs["messages"]):
            value = json.loads(result.choices[0].message.content)
            value[0]["anchor"]["part"] = "body"
            result.choices[0].message.content = json.dumps(value)
        return result

    async def acompletion(**kwargs):
        return completion(**kwargs)

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(litellm, "acompletion", acompletion)
    apply_kb_config_patch(
        kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"pdf_short_max_pages": 1})
    )
    imported = import_document(kb_dir, FIXTURE)
    assert imported.status == "failed" and "anchor" in imported.message.lower()
    source = read_document_source(kb_dir, imported.source_id)
    assert source["knowledge_revision_id"] is None
    assert source["office"]["slides"][0]["notes"].startswith("Speaker note only")
