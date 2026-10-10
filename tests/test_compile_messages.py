"""The actual compiler transport retains native sparse and partial cell observations."""

import asyncio
import json

import pytest
from compile_responses import summary_reply
from test_compile_windows import compile_boundary


def original_payload(messages, prefix):
    for message in messages:
        content = message["content"]
        blocks = content if isinstance(content, list) else [{"text": content}]
        for block in blocks:
            if block.get("text", "").startswith(prefix):
                return json.loads(block["text"].split("\n", 1)[1])
    raise AssertionError("Original payload was not sent")


def test_csv_page_input_retains_ragged_records_and_physical_lines(kb_dir, monkeypatch):
    from openkb.application.documents import import_document

    source = kb_dir / "equipment.csv"
    source.write_text('"Rotor\nAlpha",7,,41\nRotorBeta,9\n')
    requests = []
    compile_boundary(
        monkeypatch, {"entities": {"create": [{"name": "rotor", "type": "product"}]}}, requests
    )
    assert import_document(kb_dir, source).status == "added"
    page = next(r for r in requests if "Write the entity page" in str(r))
    original = original_payload(page, "Current original:")
    rows = original["csv_rows"]
    assert [(row["row"], len(row["cells"])) for row in rows] == [(1, 4), (2, 2)]
    assert [cell["column"] for cell in rows[0]["cells"]] == [1, 2, 3, 4]
    assert rows[0]["cells"][0]["observations"][0]["value"] == "Rotor\nAlpha"
    assert rows[0]["cells"][0]["observations"][0]["physical_lines"] == [1, 2]
    assert rows[0]["cells"][2]["observations"][0]["value"] == ""
    assert "RotorBeta" not in str(rows[0])


def test_partial_csv_page_input_does_not_refill_an_unread_value(kb_dir, monkeypatch):
    from openkb.agent.compiler import compile_long_doc
    from openkb.application.documents import import_document

    source = kb_dir / "equipment.csv"
    source.write_text("RotorAlpha,17\nRotorBeta,23\n")
    requests = []
    compile_boundary(monkeypatch, {}, requests)
    assert import_document(kb_dir, source).status == "added"
    retained = next((kb_dir / "wiki/sources").glob("*.content.json"))
    raw = json.loads(retained.read_text())
    first = next(origin for origin in raw["origins"] if origin.get("csv", {}).get("column") == 1)
    start = first["normalized_span"][0]
    requests.clear()
    compile_boundary(
        monkeypatch,
        {
            "entities": {
                "create": [
                    {
                        "name": "rotor",
                        "type": "product",
                        "reading_locations": {"chars": f"{start}:{start + 4}"},
                    }
                ]
            }
        },
        requests,
    )
    doc_name = retained.name.removesuffix(".content.json")
    asyncio.run(
        compile_long_doc(
            doc_name,
            kb_dir / f"wiki/summaries/{doc_name}.md",
            "retained-index",
            kb_dir,
            "test-model",
            navigation=[],
        )
    )
    page = next(r for r in requests if "Write the entity page" in str(r))
    window_message = next(
        block["text"]
        for message in page
        if isinstance(message["content"], list)
        for block in message["content"]
        if "Original source windows (bounded, frozen):\n" in block.get("text", "")
    )
    windows = json.loads(
        window_message.split("Original source windows (bounded, frozen):\n", 1)[1].split(
            "\nPrior page basis", 1
        )[0]
    )
    observation = windows[0]["csv_rows"][0]["cells"][0]["observations"][0]
    assert observation["value_complete"] is False
    assert "value" not in observation
    assert "RotorAlpha" not in window_message
    assert "RotorBeta" not in window_message


@pytest.mark.parametrize("native", ["pages", "text"])
def test_short_page_reads_each_current_original_only_once(kb_dir, monkeypatch, native):
    import hashlib

    from openkb.agent.compiler import compile_short_doc
    from openkb.text_measurement import measure_markdown
    from openkb.text_source import FrozenText, TextOrigin

    wiki = kb_dir / "wiki"
    text = "Model Alder needs supervision during winter."
    source = wiki / "sources/current.md"
    source.write_text(text)
    rich = wiki / ("sources/current.json" if native == "pages" else "sources/current.content.json")
    rich.write_text(
        json.dumps([{"page": 1, "unit_kind": "page", "content": text}])
        if native == "pages"
        else FrozenText(
            text=text,
            original_characters=len(text),
            original_digest=hashlib.sha256(source.read_bytes()).hexdigest(),
            origins=(TextOrigin(normalized_span=(0, len(text)), original_span=(0, len(text))),),
            assets={},
            tokens=measure_markdown(text),
        ).model_dump_json()
    )
    (wiki / "sources/prior.md").write_text("Model Birch requires a summer inspection.")
    (wiki / "concepts/inspection.md").write_text(
        "---\ntype: Concept\ndescription: Inspection\nsources: "
        "[sources/current.md, " + str(rich.relative_to(wiki)) + ", sources/prior.md]\n---\n"
        "# Inspection\n\nExisting inspection conditions."
    )
    requests = []
    compile_boundary(monkeypatch, {"concepts": {"update": [{"name": "inspection"}]}}, requests)
    asyncio.run(compile_short_doc("current", source, kb_dir, "test-model"))
    page = next(r for r in requests if "Update the concept page" in str(r))
    original = original_payload(page, "Current original:")
    assert original["path"] == str(rich.relative_to(wiki))
    contexts = next(
        block["text"]
        for message in page
        if isinstance(message["content"], list)
        for block in message["content"]
        if "Original source windows (bounded, frozen):\n" in block.get("text", "")
    )
    windows = json.loads(
        contexts.split("Original source windows (bounded, frozen):\n", 1)[1].split(
            "\nPrior page basis", 1
        )[0]
    )
    assert [w["path"] for w in windows] == ["sources/prior.md"]
    assert windows[0]["source_role"] == "prior_linked_source"
    assert "Model Birch" in windows[0]["content"]


@pytest.mark.parametrize("stage", ["summary", "concept", "entity"])
def test_unchanged_cannot_replace_new_knowledge_pages(kb_dir, monkeypatch, stage):
    from types import SimpleNamespace

    from openkb.application.documents import import_document

    source = kb_dir / "instructions.md"
    source.write_text("The reactor requires a documented safety inspection before start.")
    sends = []

    def response(**kwargs):
        messages = kwargs["messages"]
        sends.append(messages)
        if "Based on the summary above" in str(messages):
            result = {
                "concepts": {"create": [{"name": "reactor"}]},
                "entities": {"create": [{"name": "reactor", "type": "product"}]},
            }
        elif (stage == "summary" and "Write a summary page" in str(messages)) or (
            f"Write the {stage} page" in str(messages)
        ):
            result = {"status": "unchanged", "content": "Navigation only", "description": "Reactor"}
        else:
            result = {
                "content": "# Reactor\n\nSafety inspection before start.",
                "description": "Reactor",
            }
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(summary_reply(kwargs["messages"], result))
                    ),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    async def asynchronous(**kwargs):
        return response(**kwargs)

    monkeypatch.setattr("litellm.completion", response)
    monkeypatch.setattr("litellm.acompletion", asynchronous)
    result = import_document(kb_dir, source)
    assert result.status == "failed"
    assert (
        "summary_invalid_shape" if stage == "summary" else "page_unchanged_not_allowed"
    ) in result.quality
    assert not (kb_dir / "wiki/summaries/instructions.md").exists()
    assert not (kb_dir / "wiki/concepts/reactor.md").exists()
    assert not (kb_dir / "wiki/entities/reactor.md").exists()
    assert not list((kb_dir / ".openkb/journal").glob("*.json"))
