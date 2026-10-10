"""Application imports use native original windows, with no compulsory plan gate."""

import json
from types import SimpleNamespace

import pytest

pytest_plugins = ("test_pdf_readback",)


def compile_boundary(monkeypatch, plan, requests):
    def response(**kwargs):
        messages = kwargs["messages"]
        prompt = str(messages)
        if "Based on the summary above" in prompt:
            result = plan(messages) if callable(plan) else plan
        else:
            requests.append(messages)
            result = {"description": "Recorded section", "content": "# Record\n\nSection facts."}
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
