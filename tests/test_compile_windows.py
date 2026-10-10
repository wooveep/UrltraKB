"""Application imports use native original windows, with no compulsory plan gate."""

import json
from types import SimpleNamespace

import pytest

pytest_plugins = ("test_pdf_readback",)


def patch_page_response(messages, response):
    """Controlled provider replies follow the compiler's temporary edit protocol."""
    for message in messages:
        content = message.get("content")
        if isinstance(content, str) and content.startswith("Temporary editable page blocks:\n"):
            data = json.loads(content.split("\n", 1)[1].split("\n\n", 1)[0])
            try:
                page = json.loads(response)
            except ValueError:
                return response
            if not isinstance(page, dict) or not isinstance(page.get("content"), str):
                return response
            return json.dumps(
                {
                    "base_sha256": data["base_sha256"],
                    "description": page.get("description", page.get("brief", "")),
                    **({"type": page["type"]} if "type" in page else {}),
                    "edits": [
                        {
                            "block_id": data["blocks"][-1]["id"],
                            "operation": "insert_after",
                            "content": "\n\n" + page["content"],
                        }
                    ],
                }
            )
    return response


def compile_boundary(monkeypatch, plan, requests, page_reply=None):
    def response(**kwargs):
        messages = kwargs["messages"]
        prompt = str(messages)
        if "Based on the summary above" in prompt:
            result = plan(messages) if callable(plan) else plan
        else:
            requests.append(messages)
            result = page_reply or {
                "description": "Recorded section",
                "content": "# Record\n\nSection facts.",
            }
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=patch_page_response(messages, json.dumps(result))
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


def test_short_update_rereads_prior_originals(kb_dir, monkeypatch):
    from openkb.application.documents import import_document

    previous = kb_dir / "old.txt"
    previous.write_text("For Model A, 12 slots are recommended.\n")
    current = kb_dir / "new.txt"
    current.write_text("For Model B, 24 slots are required.\n")
    calls = []
    compile_boundary(monkeypatch, {"concepts": {"create": [{"name": "slots"}]}}, calls)
    assert import_document(kb_dir, previous).status == "added"
    calls.clear()
    compile_boundary(monkeypatch, {"concepts": {"update": [{"name": "slots"}]}}, calls)
    assert import_document(kb_dir, current).status == "added"
    update = next(m for m in calls if "Update the concept page" in str(m))
    assert "For Model A, 12 slots are recommended." in str(update)
    assert "For Model B, 24 slots are required." in str(update)
    assert "coverage" in str(update)


def test_content_only_entity_update_preserves_confirmed_type(kb_dir, monkeypatch):
    from openkb import frontmatter
    from openkb.application.documents import import_document

    previous, current = kb_dir / "old.txt", kb_dir / "new.txt"
    previous.write_text("River Research is an organization.")
    current.write_text("River Research has a new recorded location.")
    compile_boundary(
        monkeypatch,
        {"entities": {"create": [{"name": "river-research", "type": "organization"}]}},
        [],
        page_reply={
            "description": "Organization",
            "content": "# River Research\n\nAn organization.",
            "type": "organization",
        },
    )
    assert import_document(kb_dir, previous).status == "added"
    page = kb_dir / "wiki/entities/river-research.md"
    assert frontmatter.parse(page.read_text())["type"] == "Organization"
    compile_boundary(monkeypatch, {"entities": {"update": [{"name": "river-research"}]}}, [])
    assert import_document(kb_dir, current).status == "added"
    assert frontmatter.parse(page.read_text())["type"] == "Organization"


def test_navigation_only_import_keeps_fact_provenance_and_survives_removal(kb_dir, monkeypatch):
    from openkb import frontmatter
    from openkb.application.documents import import_document
    from openkb.application.removal import run_remove_for_api
    from openkb.knowledge_evidence import page_evidence
    from openkb.knowledge_scope import live_scope

    previous, current = kb_dir / "old.txt", kb_dir / "new.txt"
    previous.write_text("Recorded orchard operating conditions.")
    current.write_text("A document that points readers to the orchard topic.")
    compile_boundary(monkeypatch, {"concepts": {"create": [{"name": "orchard"}]}}, [])
    assert import_document(kb_dir, previous).status == "added"
    page = kb_dir / "wiki/concepts/orchard.md"
    before = page.read_bytes()
    evidence = page_evidence(live_scope(kb_dir), "concepts/orchard.md")
    compile_boundary(monkeypatch, {"concepts": {"related": ["orchard"]}}, [])
    assert import_document(kb_dir, current).status == "added"
    assert page.read_bytes() == before
    assert "[[concepts/orchard]]" in (kb_dir / "wiki/summaries/new.md").read_text()
    assert frontmatter.parse(page.read_text())["sources"] == ["summaries/old.md"]
    assert (
        page_evidence(live_scope(kb_dir), "concepts/orchard.md")["source_revision_ids"]
        == (evidence["source_revision_ids"])
    )
    assert run_remove_for_api(kb_dir, "new")["status"] == "removed"
    assert page.read_bytes() == before
    assert (
        page_evidence(live_scope(kb_dir), "concepts/orchard.md")["source_revision_ids"]
        == (evidence["source_revision_ids"])
    )


def test_short_import_preserves_summary_and_assembles_navigation(kb_dir, monkeypatch):
    from openkb import frontmatter
    from openkb.application.documents import import_document

    sends = []
    first = "# Capacity\n\nModel K supports 36 slots under the recorded condition."

    def response(**kwargs):
        sends.append(kwargs["messages"])
        prompt = str(kwargs["messages"])
        if "Based on the summary above" in prompt:
            result = {"concepts": {"create": [{"name": "capacity"}]}}
        elif "Write a summary page" in prompt:
            result = {"description": "Capacity condition", "content": first}
        else:
            result = {"description": "Capacity", "content": "# Capacity\n\nRecorded condition."}
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(result)), finish_reason="stop"
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    async def asynchronous(**kwargs):
        return response(**kwargs)

    monkeypatch.setattr("litellm.completion", response)
    monkeypatch.setattr("litellm.acompletion", asynchronous)
    source = kb_dir / "capacity.txt"
    source.write_text(first)
    assert import_document(kb_dir, source).status == "added"
    body = frontmatter.split((kb_dir / "wiki/summaries/capacity.md").read_text())[1]
    assert body.lstrip().startswith(first)
    assert "- [[concepts/capacity]]" in body
    assert len(sends) == 3


@pytest.mark.parametrize("repaired", [False, True])
def test_short_summary_format_repair_is_bounded_before_publication(kb_dir, monkeypatch, repaired):
    from openkb import frontmatter
    from openkb.application.documents import import_document

    summaries = []
    content = "# Capacity\n\nModel K has 36 slots under supervision."

    def response(**kwargs):
        if "Based on the summary above" in str(kwargs["messages"]):
            raw = json.dumps({"concepts": {}, "entities": {}})
        else:
            summaries.append(kwargs["messages"])
            raw = json.dumps({"description": "Capacity", "content": content})
            if not repaired or len(summaries) == 1:
                raw += '\n{"type": "json_object"}'
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=raw), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    async def asynchronous(**kwargs):
        return response(**kwargs)

    monkeypatch.setattr("litellm.completion", response)
    monkeypatch.setattr("litellm.acompletion", asynchronous)
    source = kb_dir / "capacity.txt"
    source.write_text(content)
    result = import_document(kb_dir, source)
    assert len(summaries) == 2
    summary = kb_dir / "wiki/summaries/capacity.md"
    if repaired:
        assert result.status == "added", result
        assert "page_response_repaired" in result.quality
        assert frontmatter.split(summary.read_text())[1].strip() == content
    else:
        assert result.status == "failed", result
        assert "page_multiple_objects" in result.quality
        assert not summary.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_summary_repair_releases_sdk_client_on_failure(kb_dir, monkeypatch, cancelled):
    import asyncio

    import httpx
    import litellm

    from openkb.agent.compiler import compile_short_doc
    from openkb.compilation_report import CompilationIncomplete

    raw = '{"content": "# Summary\\n\\nSupported facts."}\n{"type": "json_object"}'
    reply = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=raw), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )
    monkeypatch.setattr(litellm, "completion", lambda **kwargs: reply)
    client = httpx.AsyncClient()
    monkeypatch.setitem(
        litellm.in_memory_llm_clients_cache.cache_dict,
        "summary-repair-test-client",
        SimpleNamespace(client=client),
    )

    async def response(**kwargs):
        if cancelled:
            raise asyncio.CancelledError
        return reply

    monkeypatch.setattr(litellm, "acompletion", response)
    source = kb_dir / "wiki/sources/summary-failure.md"
    source.write_text("# Source\n\nSupported facts.")
    try:
        with pytest.raises(asyncio.CancelledError if cancelled else CompilationIncomplete):
            await compile_short_doc("summary-failure", source, kb_dir, "gpt-4o")
        assert client.is_closed
        assert not (kb_dir / "wiki/summaries/summary-failure.md").exists()
    finally:
        await client.aclose()


def test_entity_type_requires_generation_confirmation(kb_dir, monkeypatch):
    from openkb import frontmatter
    from openkb.application.documents import import_document

    source = kb_dir / "component.txt"
    source.write_text("River Valve is a flow-control component.")
    compile_boundary(
        monkeypatch,
        {
            "entities": {
                "create": [{"name": "river-valve", "title": "River Valve", "type": "organization"}]
            }
        },
        [],
    )
    assert import_document(kb_dir, source).status == "added"
    page = frontmatter.parse((kb_dir / "wiki/entities/river-valve.md").read_text())
    assert page["type"] == "Other"


def test_import_update_keeps_prior_conditions_and_adds_all_new_sections(kb_dir, monkeypatch):
    from openkb import frontmatter
    from openkb.application.documents import import_document

    old = (
        "# Conditions\n\nModel A: 12 slots during winter.\n\n"
        "| Object | Limit |\n| --- | --- |\n| Model C | 72 hours |\n"
    )
    updating = False

    def response(**kwargs):
        prompt = str(kwargs["messages"])
        if "Based on the summary above" in prompt:
            result = {"concepts": {("update" if updating else "create"): [{"name": "limits"}]}}
        elif updating:
            blocks = next(
                (
                    message["content"]
                    for message in kwargs["messages"]
                    if isinstance(message.get("content"), str)
                    and message["content"].startswith("Temporary editable page blocks:\n")
                ),
                None,
            )
            if blocks:
                data = json.loads(blocks.split("\n", 1)[1].split("\n\n", 1)[0])
                result = {
                    "base_sha256": data["base_sha256"],
                    "description": "Conditions",
                    "edits": [
                        {
                            "block_id": data["blocks"][-1]["id"],
                            "operation": "insert_after",
                            "content": "\n\nModel B: 36 slots.\n\nMaintenance: 48 hours.\n",
                        }
                    ],
                }
            else:
                result = {
                    "description": "Conditions",
                    "content": "# Conditions\n\nModel B: 36 slots.",
                }
        else:
            result = {"description": "Conditions", "content": old}
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(result)), finish_reason="stop"
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    async def asynchronous(**kwargs):
        return response(**kwargs)

    monkeypatch.setattr("litellm.completion", response)
    monkeypatch.setattr("litellm.acompletion", asynchronous)
    first = kb_dir / "winter.txt"
    first.write_text(old)
    assert import_document(kb_dir, first).status == "added"
    updating = True
    second = kb_dir / "irrigation.txt"
    second.write_text("Model B: 36 slots.\nMaintenance: 48 hours.")
    assert import_document(kb_dir, second).status == "added"
    body = frontmatter.split((kb_dir / "wiki/concepts/limits.md").read_text())[1]
    assert old in body
    assert "Model B: 36 slots." in body
    assert "Maintenance: 48 hours." in body


def test_import_blocks_proven_numeric_conflict_after_one_page_repair(kb_dir, monkeypatch):
    from openkb.application.documents import import_document

    table = (
        "# Limits\n\n| Object | Attribute | Condition | Value |\n"
        "| --- | --- | --- | --- |\n| River Valve | Latency | Winter | {} |\n"
    )
    page_sends = []
    compile_boundary(monkeypatch, {"concepts": {"create": [{"name": "latency"}]}}, [])

    async def response(**kwargs):
        page_sends.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {"description": "Latency", "content": table.format("24 ms")}
                        )
                    ),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    monkeypatch.setattr("litellm.acompletion", response)
    source = kb_dir / "latency.txt"
    source.write_text(table.format("12 ms"))
    result = import_document(kb_dir, source)
    assert result.status == "failed"
    assert "page_numeric_conflict" in result.quality
    assert not (kb_dir / "wiki/concepts/latency.md").exists()
    assert len(page_sends) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_long_block_compile_receives_only_selected_frozen_image(
    kb_dir, tmp_path, monkeypatch, changed
):
    from PIL import Image

    from openkb.agent.compiler import _compile_concepts
    from openkb.agent.source_windows import CompileSourceWindows
    from openkb.block_package import freeze_block_package
    from openkb.inputs import prepared_input
    from openkb.llm_images import request_image_digests
    from openkb.locks import atomic_write_json, atomic_write_text
    from openkb.text_source import freeze_markdown

    for name, color in [("first.png", "red"), ("other.png", "blue")]:
        Image.new("RGB", (4, 4), color).save(tmp_path / name)
    source = tmp_path / "manual.md"
    source.write_text(
        "# Selected\n\n![Selected figure](first.png)\n\n"
        "# Unrelated\n\n![Unrelated figure](other.png)\n",
        encoding="utf-8",
    )
    wiki = kb_dir / "wiki"
    with prepared_input(source) as ready:
        frozen = freeze_markdown(ready, "manual", wiki)
    path = wiki / "sources/manual.md"
    atomic_write_text(path, frozen.text)
    atomic_write_json(path.with_suffix(".content.json"), frozen.model_dump(mode="json"))
    freeze_block_package(path, "manual")
    end = frozen.text.index("# Unrelated")
    item = {"name": "selected", "reading_locations": {"chars": f"0:{end}"}}
    if changed:
        from openkb.agent.page_response import PageResponseError

        Image.new("RGB", (4, 4), "green").save(wiki / "sources/images/manual/first.png")
        with pytest.raises(PageResponseError, match="changed"):
            CompileSourceWindows(wiki, "manual").prepare(item, long=True)
        return
    requests = []
    compile_boundary(
        monkeypatch,
        {"concepts": {"create": [item]}},
        requests,
    )
    await _compile_concepts(
        wiki,
        kb_dir,
        "test-model",
        {"role": "system", "content": "You are a wiki compilation agent."},
        {"role": "user", "content": "Long source navigation; read selected originals."},
        "Figure navigation",
        "manual",
        1,
        doc_type="pageindex",
        source_windows=CompileSourceWindows(wiki, "manual"),
    )
    import hashlib

    assert request_image_digests(requests) == {
        hashlib.sha256((tmp_path / "first.png").read_bytes()).hexdigest()
    }


@pytest.mark.parametrize("suggestions", [None, {"pages": "3"}, {"pages": [3]}, {"pages": "9"}])
def test_long_import_reads_native_windows_without_requiring_suggestions(
    kb_dir, physical_pdf, pdf_model, monkeypatch, suggestions
):
    from openkb.application.documents import import_document
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest

    apply_kb_config_patch(
        kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"pdf_short_max_pages": 0})
    )
    # PageIndex retains its real algorithm and this fixture's provider boundary.
    import litellm

    index_response = litellm.completion
    requests = []
    item = {"name": "last-section", "title": "Last section"}
    if suggestions:
        item["reading_locations"] = suggestions
    compile_boundary(monkeypatch, {"concepts": {"create": [item]}}, requests)
    compile_response = litellm.completion

    def routed(**kwargs):
        prompt = str(kwargs["messages"])
        if any(s in prompt for s in ("wiki compilation agent", "Original source windows")):
            return compile_response(**kwargs)
        return index_response(**kwargs)

    async def async_routed(**kwargs):
        return routed(**kwargs)

    monkeypatch.setattr("litellm.completion", routed)
    monkeypatch.setattr("litellm.acompletion", async_routed)
    imported = import_document(kb_dir, physical_pdf)
    if suggestions == {"pages": "9"}:
        assert imported.status == "failed"
        assert not (kb_dir / "wiki/concepts/last-section.md").exists()
        return
    assert imported.status == "added", imported
    page = next(m for m in requests if "Write the concept page" in str(m))
    assert "Physical page three." in str(page)
    assert "physical pages" in str(page)
    assert "coverage" in str(page)
    assert "sha256" in str(page)


def test_short_update_rereads_a_published_long_source(kb_dir, physical_pdf, pdf_model, monkeypatch):
    from openkb.application.documents import import_document
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest

    apply_kb_config_patch(
        kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"pdf_short_max_pages": 0})
    )
    import litellm

    index_response = litellm.completion
    requests = []
    compile_boundary(monkeypatch, {"concepts": {"create": [{"name": "last-section"}]}}, requests)
    compile_response = litellm.completion

    def routed(**kwargs):
        return (
            compile_response
            if "wiki compilation agent" in str(kwargs["messages"])
            else index_response
        )(**kwargs)

    async def asynchronous(**kwargs):
        return routed(**kwargs)

    monkeypatch.setattr("litellm.completion", routed)
    monkeypatch.setattr("litellm.acompletion", asynchronous)
    assert import_document(kb_dir, physical_pdf).status == "added"
    source = kb_dir / "new.txt"
    source.write_text("Last section: an additional condition is recorded here.")
    requests.clear()
    compile_boundary(
        monkeypatch,
        {"concepts": {"update": [{"name": "last-section", "title": "Last section"}]}},
        requests,
    )
    assert import_document(kb_dir, source).status == "added"
    update = next(m for m in requests if "Update the concept page" in str(m))
    assert "Physical page three." in str(update)
    assert "additional condition" in str(update)
