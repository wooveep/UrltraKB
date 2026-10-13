"""Response transcription uses the same qualified originals as public readers."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from compile_responses import summary_reply
from test_answer_evidence_regression import agent_for, invoke
from test_native_table_projection import native_page
from test_source_qualification import freeze_source

from openkb.agent.compiler import compile_long_doc, compile_short_doc
from openkb.agent.document_summary import build_document_summary
from openkb.agent.evidence_payload import project_evidence, slice_evidence
from openkb.agent.page_response import PageResponseError
from openkb.agent.source_excerpts import SourceExcerpts
from openkb.agent.source_windows import CompileSourceWindows
from openkb.application.documents import import_document
from openkb.locks import atomic_write_text
from openkb.text_source import read_text_selection

LITERAL_PATH = "/var/&lt;target&gt;/state"
VISIBLE_PATH = "/var/<target>/state"
TABLE = f"<table><tr><td>{LITERAL_PATH}</td></tr></table>"


@pytest.fixture
def transcription_model(monkeypatch):
    generations = []

    def completion(**kwargs):
        messages = kwargs["messages"]
        if "Based on the summary above" in str(messages):
            value = {}
        else:
            generations.append(messages)
            chosen = VISIBLE_PATH if len(generations) == 1 else LITERAL_PATH
            value = {"description": "Printed syntax", "content": f"Inspect `{chosen}`."}
            value = summary_reply(messages, value)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(value)), finish_reason="stop"
                )
            ],
            usage=None,
        )

    async def acompletion(**kwargs):
        return completion(**kwargs)

    monkeypatch.setenv("LLM_API_KEY", "fixture-unused")
    monkeypatch.setattr("litellm.completion", completion)
    monkeypatch.setattr("litellm.acompletion", acompletion)
    return generations


@pytest.mark.parametrize("format,qualified", [("pdf", False), ("md", True)])
def test_response_contract_agrees_in_import_and_question(
    kb_dir, tmp_path, transcription_model, format, qualified
):
    import pymupdf

    atomic_write_text(kb_dir / ".openkb/config.yaml", "model: openai/gpt-4o-mini\nlanguage: en\n")
    path = tmp_path / f"manual.{format}"
    if format == "pdf":
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((60, 70), TABLE, fontsize=10)
            pdf.save(path)
    else:
        atomic_write_text(path, TABLE)
    original = path.read_bytes()
    result = import_document(kb_dir, path)
    assert result.status == "added", result.message
    assert len(transcription_model) == (1 if qualified else 2)
    if not qualified:
        assert "page_literal_conflict" in str(transcription_model[1])
    summary = (kb_dir / "wiki/summaries/manual.md").read_text()
    assert (VISIBLE_PATH if qualified else LITERAL_PATH) in summary
    reader = "get_text_content" if qualified else "get_page_content"
    arguments = {"chars": f"0:{len(TABLE)}"} if qualified else {"pages": "1"}
    response = asyncio.run(invoke(agent_for(kb_dir), reader, doc_name="manual", **arguments))
    public = json.loads(response.split("\n\n", 1)[1].split("\nSource context: ", 1)[0])
    assert len(public.get("native_tables", [])) == int(qualified)
    assert LITERAL_PATH in public["content"]
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "format,qualified", [("native", True), ("legacy", False), ("md", True), ("txt", False)]
)
def test_response_contract_agrees_in_segmented_compile_and_question(
    kb_dir, tmp_path, transcription_model, format, qualified
):
    wiki = kb_dir / "wiki"
    if format in {"native", "legacy"}:
        source = wiki / "sources/manual.json"
        page = (
            native_page(TABLE) if qualified else {"page": 1, "unit_kind": "page", "content": TABLE}
        )
        atomic_write_text(source, json.dumps([page]))
        reader, arguments = "get_page_content", {"pages": "1"}
    else:
        _, frozen = freeze_source(kb_dir, tmp_path, format, TABLE)
        source = wiki / "sources/manual.content.json"
        reader, arguments = "get_text_content", {"chars": f"0:{len(frozen.text)}"}
    original = source.read_bytes()
    summary = wiki / "summaries/manual.md"
    asyncio.run(compile_long_doc("manual", summary, "saved", kb_dir, "test-model", navigation=[]))
    assert len(transcription_model) == (1 if qualified else 2)
    assert (VISIBLE_PATH if qualified else LITERAL_PATH) in summary.read_text()
    public = asyncio.run(invoke(agent_for(kb_dir), reader, doc_name="manual", **arguments))
    payload = json.loads(public.split("\n\n", 1)[1].split("\nSource context: ", 1)[0])
    assert len(payload.get("native_tables", [])) == int(qualified)
    if qualified:
        cell = payload["native_tables"][0]["cells"][0]
        assert cell["value"] == VISIBLE_PATH and cell["value_coverage"] == "complete"
    assert source.read_bytes() == original


@pytest.mark.parametrize(
    "body,partial,accepted",
    [
        (LITERAL_PATH, False, True),
        (f"<span>{LITERAL_PATH}</span>", False, True),
        (LITERAL_PATH, True, False),
        (f"<input value='42'>{LITERAL_PATH}", False, False),
        ("/var/&lt;tar<br>get&gt;/state", False, False),
        ("<p>/var/&lt;tar</p><p>get&gt;/state</p>", False, False),
    ],
)
def test_response_requires_a_complete_resolved_cell(kb_dir, tmp_path, body, partial, accepted):
    text = f"<table><tr><td>{body}</td></tr></table>"
    _, frozen = freeze_source(kb_dir, tmp_path, "md", text)
    selected = read_text_selection(frozen.model_dump(mode="json"))
    evidence = (
        slice_evidence(selected, 0, selected["content"].index("</td>"))
        if partial
        else project_evidence(selected)
    )
    excerpts = SourceExcerpts([evidence])
    response = f"Inspect `{VISIBLE_PATH}`."
    if accepted:
        assert excerpts.render(response) == response
    else:
        with pytest.raises(PageResponseError, match="does not occur verbatim"):
            excerpts.render(response)


def test_unqualified_legacy_text_keeps_exact_excerpt_without_decoding():
    excerpts = SourceExcerpts([TABLE])
    with pytest.raises(PageResponseError, match="does not occur verbatim"):
        excerpts.render(f"Inspect `{VISIBLE_PATH}`.")
    assert TABLE in excerpts.render("[[original:x1.1]]")
    assert excerpts.render(f"Inspect `{LITERAL_PATH}`.") == f"Inspect `{LITERAL_PATH}`."


@pytest.mark.parametrize("qualified", [True, False])
@pytest.mark.parametrize("segmented", [True, False])
@pytest.mark.parametrize("kind", ["concepts", "entities"])
def test_new_pages_consume_the_same_response_proof(kb_dir, monkeypatch, qualified, segmented, kind):
    from openkb.locks import atomic_write_json

    wiki = kb_dir / "wiki"
    source = wiki / "sources/manual.md"
    atomic_write_text(source, TABLE)
    page = native_page(TABLE) if qualified else {"page": 1, "unit_kind": "page", "content": TABLE}
    atomic_write_json(wiki / "sources/manual.json", [page])
    generations = []

    def completion(**kwargs):
        messages = kwargs["messages"]
        prompt = str(messages)
        if "Based on the summary above" in prompt:
            value = {
                kind: {
                    "create": [
                        {
                            "name": "rules",
                            "title": "Rules",
                            "type": "other",
                            "reading_locations": {"pages": "1"},
                        }
                    ]
                }
            }
        elif "Write a summary page for this document" in prompt or "Original document: " in prompt:
            value = summary_reply(messages, {"description": "Syntax", "content": "Printed syntax."})
        else:
            generations.append(messages)
            chosen = VISIBLE_PATH if len(generations) == 1 else LITERAL_PATH
            value = {"description": "Rule", "content": f"Read `{chosen}`.", "type": "other"}
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(value)), finish_reason="stop"
                )
            ],
            usage=None,
        )

    async def acompletion(**kwargs):
        return completion(**kwargs)

    monkeypatch.setenv("LLM_API_KEY", "fixture-unused")
    monkeypatch.setattr("litellm.completion", completion)
    monkeypatch.setattr("litellm.acompletion", acompletion)
    if segmented:
        asyncio.run(
            compile_long_doc(
                "manual", wiki / "summaries/manual.md", "saved", kb_dir, "test-model", navigation=[]
            )
        )
    else:
        asyncio.run(compile_short_doc("manual", source, kb_dir, "test-model"))
    assert len(generations) == (1 if qualified else 2)
    assert (VISIBLE_PATH if qualified else LITERAL_PATH) in (wiki / kind / "rules.md").read_text()
    assert source.read_text() == TABLE


@pytest.mark.parametrize("qualified", [True, False])
def test_saved_summary_response_uses_current_source_proof_without_regeneration(kb_dir, qualified):
    from openkb.locks import atomic_write_json

    source = kb_dir / "wiki/sources/manual.json"
    page = native_page(TABLE) if qualified else {"page": 1, "unit_kind": "page", "content": TABLE}
    atomic_write_json(source, [page])
    original = source.read_bytes()
    saved = {
        "description": "Saved",
        "sections": [{"id": "section-1", "content": f"Inspect `{VISIBLE_PATH}`."}],
    }
    before = json.dumps(saved)

    class ReadOnlyCheckpoint:
        def load(self, key, signature):
            assert key == "summary:1"
            return saved

    async def unexpected(*args):
        pytest.fail("Reading an accepted response must not regenerate it")

    async def compile_saved():
        return await build_document_summary(
            CompileSourceWindows(kb_dir / "wiki", "manual"),
            system_message={"role": "system", "content": "Compile"},
            generate=unexpected,
            fits=lambda _: True,
            checkpoints=ReadOnlyCheckpoint(),
        )

    if qualified:
        assert VISIBLE_PATH in asyncio.run(compile_saved()).markdown
    else:
        with pytest.raises(PageResponseError, match="does not occur verbatim"):
            asyncio.run(compile_saved())
    assert json.dumps(saved) == before and source.read_bytes() == original


@pytest.mark.parametrize("qualified", [True, False])
def test_saved_short_summary_uses_the_same_proof_without_regeneration(
    kb_dir, monkeypatch, qualified
):
    from openkb.compilation_report import CompilationIncomplete
    from openkb.locks import atomic_write_json

    wiki = kb_dir / "wiki"
    source = wiki / "sources/manual.md"
    atomic_write_text(source, TABLE)
    page = native_page(TABLE) if qualified else {"page": 1, "unit_kind": "page", "content": TABLE}
    atomic_write_json(wiki / "sources/manual.json", [page])
    saved = json.dumps({"description": "Saved", "content": f"Inspect `{VISIBLE_PATH}`."})

    class ReadOnlyCheckpoint:
        def load(self, key, signature):
            assert key in {"short-summary", "plan"}
            return saved if key == "short-summary" else "{}"

    calls = []

    def completion(**kwargs):
        assert "Based on the summary above" in str(kwargs["messages"])
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="{}"), finish_reason="stop")],
            usage=None,
        )

    monkeypatch.setattr(
        "openkb.compile_checkpoints.document_checkpoints", lambda *args: ReadOnlyCheckpoint()
    )
    monkeypatch.setattr("litellm.completion", completion)
    if qualified:
        asyncio.run(compile_short_doc("manual", source, kb_dir, "test-model"))
        assert VISIBLE_PATH in (wiki / "summaries/manual.md").read_text()
    else:
        with pytest.raises(CompilationIncomplete, match="does not occur verbatim"):
            asyncio.run(compile_short_doc("manual", source, kb_dir, "test-model"))
        assert not (wiki / "summaries/manual.md").exists()
    assert not calls
    assert source.read_text() == TABLE


@pytest.mark.parametrize("qualified", [True, False])
@pytest.mark.parametrize("cached", [True, False])
def test_page_update_response_uses_the_same_cell_proof(kb_dir, monkeypatch, qualified, cached):
    from openkb.agent.page_updates import PageUpdate
    from openkb.agent.update_generation import generate_page_update
    from openkb.locks import atomic_write_json

    wiki = kb_dir / "wiki"
    source = wiki / "sources/manual.json"
    page = native_page(TABLE) if qualified else {"page": 1, "unit_kind": "page", "content": TABLE}
    atomic_write_json(source, [page])
    target = wiki / "concepts/rules.md"
    old = "Old recovery rule.\n"
    atomic_write_text(target, old)
    windows = CompileSourceWindows(wiki, "manual", kb_dir=kb_dir)
    originals = windows.prepare({"reading_locations": {"pages": "1"}}, long=True)
    patch = PageUpdate(old, originals=windows.numeric_sources(originals, long=True))
    value = {
        "base_sha256": patch.digest,
        "edits": [{"block_id": "b1", "operation": "replace", "content": f"Read `{VISIBLE_PATH}`."}],
    }
    original_value = json.dumps(value)
    calls = []

    class ReadOnlyCheckpoint:
        def bind_page(self, *args, **kwargs):
            pass

        def load(self, key, signature):
            return value

    if cached:
        monkeypatch.setattr(
            "openkb.agent.update_generation.document_checkpoints",
            lambda *args: ReadOnlyCheckpoint(),
        )

    async def completion(**kwargs):
        calls.append(kwargs)
        chosen = VISIBLE_PATH if len(calls) == 1 else LITERAL_PATH
        response = {**value, "edits": [{**value["edits"][0], "content": f"Read `{chosen}`."}]}
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(response)), finish_reason="stop"
                )
            ],
            usage=None,
        )

    monkeypatch.setattr("litellm.acompletion", completion)

    async def generate():
        return await generate_page_update(
            patch=patch,
            originals=originals,
            windows=windows,
            page_path=target,
            model="test-model",
            bundle=None,
            system_message={"role": "system", "content": "Compile"},
            current_original=None,
            known_targets_message={"role": "user", "content": "rules"},
            task_message={"role": "user", "content": "Update rules"},
            fits=lambda _: True,
        )

    if cached and not qualified:
        with pytest.raises(PageResponseError, match="does not occur verbatim"):
            asyncio.run(generate())
    else:
        result = asyncio.run(generate())
        assert (VISIBLE_PATH if qualified else LITERAL_PATH) in result["content"]
    assert len(calls) == (0 if cached else 1 if qualified else 2)
    assert target.read_text() == old and json.dumps(value) == original_value
