"""Content blocks retain original coordinates through the public import/read seam."""

import json
from pathlib import Path

import pytest

pytest_plugins = ("block_fixtures",)


def test_short_text_can_use_blocks_and_recompile_its_retained_original(
    kb_dir, tmp_path, block_model
):
    from openkb.application.documents import import_document
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.documents import read_document_source

    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir), config={"model": "gpt-4o", "model_capacity": {"max_input_tokens": 1}}
        ),
    )
    source = tmp_path / "greeting.md"
    source.write_bytes(b"hello world\r\n")
    result = import_document(kb_dir, source)
    assert result.status == "added", result.message
    saved = read_document_source(kb_dir, result.source_id, blocks="1")
    assert saved["content"] == "hello world\r\n" and saved["pages"] is None
    assert saved["block_count"] == 1 and saved["length_class"] == "short"
    assert saved["execution_mode"] == "segmented"
    assert saved["units"][0]["source_spans"] == [[0, 13]]
    assert any("CONTENT BLOCK STRUCTURE" in prompt for prompt in block_model)
    from click.testing import CliRunner

    from openkb.cli import cli

    output = CliRunner().invoke(
        cli, ["--kb-dir", str(kb_dir), "source", result.source_id, "--blocks", "1"]
    )
    assert output.exit_code == 0, output.output
    assert json.loads(output.output)["block_range"] == [1]
    from openkb.application.recompilation import recompile_document, select_recompilation

    source.unlink()
    selection = select_recompilation(kb_dir, "greeting", confirmation=True)
    import asyncio

    recompiled = asyncio.run(
        recompile_document(kb_dir, selection.targets[0].file_hash, version=selection.version)
    )
    assert recompiled.status == "compiled", recompiled.message
    again = read_document_source(kb_dir, result.source_id, blocks="1")
    assert again["content"] == saved["content"]
    assert again["processing"] == saved["processing"]


def test_block_package_recovers_full_tree_ranges_and_assets_without_external_inputs(
    kb_dir, tmp_path, block_model
):
    import shutil
    import sqlite3
    from pathlib import Path

    from pageindex import IndexConfig

    from openkb.application.documents import import_document
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.block_package import create_index_client
    from openkb.condb_storage import ConDBPageIndexStorage
    from openkb.documents import read_document_source
    from openkb.index_location import IndexLocation

    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir), config={"model": "gpt-4o", "model_capacity": {"max_input_tokens": 1}}
        ),
    )
    source = tmp_path / "portable.md"
    source.write_bytes(b"hello world\r\n\r\n![image](kept.png)\r\n")
    (tmp_path / "kept.png").write_bytes(b"fixed-picture-bytes")
    result = import_document(kb_dir, source)
    assert result.status == "added", result.message
    saved = read_document_source(kb_dir, result.source_id)
    package = kb_dir / saved["base_path"] / "portable.okbi"
    store = tmp_path / "index"
    with ConDBPageIndexStorage(IndexLocation.package(store)) as database:
        client = create_index_client(
            storage_path=str(store),
            storage=database,
            model="gpt-4o",
            index_config=IndexConfig(if_add_node_summary=True),
        )
        identity = client.collection().add(str(package))
    moved = tmp_path / "moved-index"
    shutil.copytree(store, moved)
    shutil.rmtree(store)
    package.unlink()
    source.unlink()
    (tmp_path / "kept.png").unlink()
    with ConDBPageIndexStorage(IndexLocation.package(moved)) as database:
        client = create_index_client(storage_path=str(moved), storage=database, model="gpt-4o")
        collection = client.collection()
        before = collection.get_document(identity, include_text=True)
        blocks = collection.get_block_content(identity, "1")
        assert before["page_count"] is None and before["block_count"] == 1
        assert before["structure"][0]["title_origin"] == "generated"
        assert before["structure"][0]["anchor"]["excerpt"] == "hello"
        assert before["structure"][0]["text"] == saved["content"]
        asset = next(iter(blocks[0]["assets"].values()))
        assert Path(asset["path"]).read_bytes() == b"fixed-picture-bytes"
        requests = len(block_model)
        with sqlite3.connect(moved / "context.sqlite") as db:
            db.execute(
                "UPDATE okb_documents SET payload = json_set(payload, '$.pages', NULL) "
                "WHERE doc_id = ?",
                (identity,),
            )
        assert collection.get_document(identity, include_text=True) == before
        assert collection.get_block_content(identity, "1") == blocks
        assert len(block_model) == requests


def test_structural_blocks_cover_code_tables_and_unicode_without_adding_display_text(
    kb_dir, block_model
):
    import unicodedata

    from openkb.application.documents import import_document
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.documents import read_document_source

    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir), config={"model": "gpt-4o", "model_capacity": {"max_input_tokens": 1}}
        ),
    )
    source = Path(__file__).parent / "fixtures/content-blocks.md"
    original = source.read_bytes().decode("utf-8")
    result = import_document(kb_dir, source)
    assert result.status == "added", result.message
    saved = read_document_source(kb_dir, result.source_id)
    assert saved["content"] == original and saved["block_count"] > 2
    cursor = 0
    supplements = set()
    for unit in saved["units"]:
        start, end = unit["source_spans"][0]
        assert start == cursor and unit["content"] == original[start:end]
        assert not unicodedata.combining(unit["content"][0])
        assert not unit["overlap_spans"]
        supplements.update(item["kind"] for item in unit["display_context"])
        cursor = end
    assert cursor == len(original)
    assert supplements == {"heading_path", "fence_open", "fence_close", "table_header"}
    last = read_document_source(kb_dir, result.source_id, blocks=str(saved["block_count"]))
    assert last["content"] == saved["units"][-1]["content"]


@pytest.mark.parametrize("corrected", [True, False])
def test_generated_section_labels_must_resolve_to_real_original_anchors(
    kb_dir, tmp_path, block_model, monkeypatch, corrected
):
    import litellm

    from openkb.application.documents import import_document
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.documents import read_document_source

    original_completion = litellm.completion

    def completion(**kwargs):
        result = original_completion(**kwargs)
        prompt = str(kwargs["messages"])
        if "CONTENT BLOCK STRUCTURE" in prompt and (not corrected or "Correct ONLY" not in prompt):
            entries = json.loads(result.choices[0].message.content)
            entries[0]["anchor"]["excerpt"] = "Invented evidence"
            result.choices[0].message.content = json.dumps(entries)
        return result

    async def acompletion(**kwargs):
        return completion(**kwargs)

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(litellm, "acompletion", acompletion)
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir), config={"model": "gpt-4o", "model_capacity": {"max_input_tokens": 1}}
        ),
    )
    path = tmp_path / "anchors.md"
    path.write_text("hello world", encoding="utf-8")
    result = import_document(kb_dir, path)
    assert result.status == ("added" if corrected else "failed"), result.message
    assert any("Correct ONLY" in prompt for prompt in block_model)
    if not corrected:
        assert sum("Correct ONLY" in prompt for prompt in block_model) == 1
        assert "BlockContractError" in result.message
    saved = read_document_source(kb_dir, result.source_id, blocks="1")
    assert saved["content"] == "hello world"


def test_unheaded_original_anchor_can_normalize_its_label_without_model_repair(
    kb_dir, tmp_path, block_model, monkeypatch
):
    import litellm

    from openkb.application.documents import import_document
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.documents import read_document_source

    original_completion = litellm.completion

    def completion(**kwargs):
        result = original_completion(**kwargs)
        if "CONTENT BLOCK STRUCTURE" in str(kwargs["messages"]):
            entries = json.loads(result.choices[0].message.content)
            entries[0]["title_origin"] = "original"
            result.choices[0].message.content = json.dumps(entries)
        return result

    async def acompletion(**kwargs):
        return completion(**kwargs)

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(litellm, "acompletion", acompletion)
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir), config={"model": "gpt-4o", "model_capacity": {"max_input_tokens": 1}}
        ),
    )
    path = tmp_path / "unheaded.md"
    path.write_text("hello world", encoding="utf-8")
    result = import_document(kb_dir, path)
    assert result.status == "added", result.message
    assert not any("Correct ONLY" in prompt for prompt in block_model)
    saved = read_document_source(kb_dir, result.source_id, blocks="1")
    assert saved["content"] == "hello world"


def test_declared_heading_rejects_a_fabricated_original_title():
    from pageindex.index.block_policy import BlockPolicy

    text = "Real heading\nhello"
    policy = BlockPolicy(
        {
            "unit_count": 1,
            "source": {
                "text": text,
                "blocks": [
                    {
                        "ordinal": 1,
                        "source_spans": [[0, len(text)]],
                        "headings": [{"title": "Real heading", "source_span": [0, 12]}],
                    }
                ],
            },
        }
    )
    entry = {
        "structure": "1",
        "title": "Invented heading",
        "title_origin": "original",
        "physical_index": 1,
        "anchor": {"unit": 1, "part": "body", "range": [13, 18], "excerpt": "hello"},
    }
    assert not policy.normalize_title_origin(entry)
    assert not policy.valid(entry)
    assert policy.validate_entry(entry) == "undeclared_original_title"


def test_unicode_line_separator_does_not_move_markdown_heading_coordinates(
    kb_dir, tmp_path, block_model
):
    from openkb.application.documents import import_document
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.documents import read_document_source

    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir), config={"model": "gpt-4o", "model_capacity": {"max_input_tokens": 1}}
        ),
    )
    path = tmp_path / "unicode.md"
    path.write_text("hello\u2028world\n# 正文\n内容\n", encoding="utf-8")
    result = import_document(kb_dir, path)
    assert result.status == "added", result.message
    saved = read_document_source(kb_dir, result.source_id, blocks="1")
    heading = saved["units"][0]["headings"][0]
    assert heading["title"] == "正文" and heading["source_span"] == [12, 17]


@pytest.mark.parametrize("part,context", [("code", "fence_open"), ("table", "table_header")])
def test_nested_structures_keep_display_context_when_their_original_spans_split(
    kb_dir, tmp_path, block_model, part, context
):
    from openkb.application.documents import import_document
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.documents import read_document_source

    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir), config={"model": "gpt-4o", "model_capacity": {"max_input_tokens": 1}}
        ),
    )
    sample = (Path(__file__).parent / "fixtures/content-blocks.md").read_text("utf-8")
    natural = (
        "```python" + sample.split("```python", 1)[1].split("```", 1)[0] + "```"
        if part == "code"
        else "| State" + sample.split("| State", 1)[1].split("\n\n", 1)[0]
    )
    original = "hello\n\n" + "\n".join("> " + line for line in natural.splitlines()) + "\n"
    path = tmp_path / "nested.md"
    path.write_text(original, encoding="utf-8")
    result = import_document(kb_dir, path)
    assert result.status == "added", result.message
    saved = read_document_source(kb_dir, result.source_id)
    assert saved["content"] == original
    assert any(
        item["kind"] == context for unit in saved["units"] for item in unit["display_context"]
    )


def test_query_reads_content_blocks_with_pinned_original_ranges(
    kb_dir, tmp_path, block_model, monkeypatch
):
    import asyncio

    from litellm import ModelResponse

    from openkb.agent.query import run_query
    from openkb.application.documents import import_document
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest

    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir), config={"model": "gpt-4o", "model_capacity": {"max_input_tokens": 1}}
        ),
    )
    path = tmp_path / "greeting.md"
    path.write_text("hello world", encoding="utf-8")
    imported = import_document(kb_dir, path)
    assert imported.status == "added", imported.message

    async def model(**kwargs):
        from evidence_model import provider_review

        if reviewed := provider_review(kwargs):
            return reviewed
        outputs = [item["content"] for item in kwargs["messages"] if item["role"] == "tool"]
        message = (
            {"content": outputs[-1]}
            if outputs
            else {
                "tool_calls": [
                    {
                        "id": "read-block",
                        "type": "function",
                        "function": {
                            "name": "get_block_content",
                            "arguments": json.dumps(
                                {
                                    "doc_name": "greeting",
                                    "blocks": "1",
                                    "view_id": imported.units[0].view_id,
                                }
                            ),
                        },
                    }
                ]
            }
        )
        return ModelResponse(
            choices=[
                {
                    "message": {"role": "assistant", **message},
                    "finish_reason": "stop" if outputs else "tool_calls",
                }
            ]
        )

    monkeypatch.setattr("litellm.acompletion", model)
    answer = asyncio.run(run_query("Read the first original block", kb_dir, "gpt-4o"))
    assert '"content": "hello world"' in answer
    assert '"source_spans": [[0, 11]]' in answer and imported.source_revision_id in answer


def test_model_correction_cannot_choose_a_different_trusted_section_index(
    kb_dir, tmp_path, block_model, monkeypatch
):
    import litellm

    from openkb.application.documents import import_document
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.block_package import create_index_client
    from openkb.condb_storage import ConDBPageIndexStorage
    from openkb.documents import read_document_source
    from openkb.index_location import IndexLocation

    original_completion = litellm.completion

    def completion(**kwargs):
        response = original_completion(**kwargs)
        prompt = str(kwargs["messages"])
        if "CONTENT BLOCK STRUCTURE" in prompt:
            if "Correct ONLY" in prompt:
                entries = [
                    {
                        "structure": "1",
                        "title": "Greeting",
                        "title_origin": "generated",
                        "physical_index": 1,
                        "list_index": 1,
                        "is_valid": True,
                        "anchor": {"unit": 1, "part": "body", "range": [0, 5], "excerpt": "hello"},
                    }
                ]
            else:
                entries = [
                    {
                        "structure": "1",
                        "title": "Unverified section",
                        "title_origin": "generated",
                        "physical_index": 1,
                        "anchor": {"unit": 1, "part": "body", "range": [0, 5], "excerpt": "FAKEX"},
                    },
                    {
                        "structure": "2",
                        "title": "World",
                        "title_origin": "generated",
                        "physical_index": 1,
                        "anchor": {"unit": 1, "part": "body", "range": [6, 11], "excerpt": "world"},
                    },
                ]
            response.choices[0].message.content = json.dumps(entries)
        return response

    async def acompletion(**kwargs):
        return completion(**kwargs)

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(litellm, "acompletion", acompletion)
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir), config={"model": "gpt-4o", "model_capacity": {"max_input_tokens": 1}}
        ),
    )
    path = tmp_path / "correction.md"
    path.write_text("hello world", encoding="utf-8")
    result = import_document(kb_dir, path)
    assert result.status == "added", result.message
    saved = read_document_source(kb_dir, result.source_id)
    store = (kb_dir / saved["base_path"]).parent.parent / "index"
    with ConDBPageIndexStorage(IndexLocation.package(store)) as database:
        collection = create_index_client(
            storage_path=str(store), storage=database, model="gpt-4o"
        ).collection()
        tree = collection.get_document(collection.list_documents()[0]["doc_id"])["structure"]
        assert [node["anchor"]["excerpt"] for node in tree] == ["hello", "world"]
