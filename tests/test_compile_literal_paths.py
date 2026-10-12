"""Public compilation must not publish invented standalone path spellings."""

import json
from types import SimpleNamespace

import pytest

pytest_plugins = ("test_vendor_sdk",)


@pytest.mark.parametrize(
    "inline",
    [
        "https://example.test/a",
        "//example.test/a",
        "../concepts/a.md",
        "sources/a.md",
        "192.0.2.0/24",
        "/24",
        "/2/3",
        "6 / 2",
        "/usr/bin/check --verbose",
        r"C:\Tools\check.exe --verbose",
        r"C:\Tools\check.exe argument",
    ],
)
def test_non_path_inline_expressions_are_not_treated_as_path_transcriptions(inline):
    from openkb.agent.source_excerpts import SourceExcerpts

    content = f"Example: `{inline}`."
    assert SourceExcerpts(["Ordinary source prose."]).render(content) == content


@pytest.mark.parametrize(
    "source,path",
    [
        ("Read /system/stateful.", "/system/state"),
        ("Read /system/state.ini.", "/system/state"),
        ("Read /home/cafe\u0301/data.", "/home/cafe"),
        ("Read /prefix/system/state.", "/system/state"),
        ("Read /system/\nstate.", "/system/state"),
        ("```text\n/system/&lt;node&gt;/state\n```", "/system/<node>/state"),
        ("<!-- /system/&lt;node&gt;/state -->", "/system/<node>/state"),
        (
            '<table data-path="/system/&lt;node&gt;/state"><tr><td>Other</td></tr></table>',
            "/system/<node>/state",
        ),
        (
            "<table><tr><td><noscript>/system/&lt;node&gt;/state</noscript></td></tr></table>",
            "/system/<node>/state",
        ),
        (
            "<table><tr><td><title>/system/&lt;node&gt;/state</title></td></tr></table>",
            "/system/<node>/state",
        ),
        (
            "<table><tr><td>/system/&lt;node&gt;/<br/>state</td></tr></table>",
            "/system/<node>/state",
        ),
    ],
)
def test_other_spellings_and_extraction_breaks_do_not_certify_a_path(source, path):
    from openkb.agent.page_response import PageResponseError
    from openkb.agent.source_excerpts import SourceExcerpts

    with pytest.raises(PageResponseError, match="does not occur verbatim"):
        SourceExcerpts([source]).render(f"Read `{path}`.")


def test_missing_window_cannot_use_old_page_paths_as_original_evidence():
    from openkb.agent.page_response import PageResponseError
    from openkb.agent.source_excerpts import SourceExcerpts

    excerpts = SourceExcerpts([])
    response = json.dumps({"description": "Recovery", "content": "Read `/system/state`."})
    with pytest.raises(PageResponseError, match="does not occur verbatim"):
        excerpts.response(response, "page-json")


@pytest.mark.parametrize("ending", [".", ". Next sentence.", ", then continue.", "。"])
def test_sentence_punctuation_is_not_part_of_the_source_path(ending):
    from openkb.agent.source_excerpts import SourceExcerpts

    content = "During recovery inspect `/system/config/state`."
    excerpts = SourceExcerpts(["During recovery inspect /system/config/state" + ending])
    assert excerpts.render(content) == content


def test_complete_path_next_to_a_native_cell_break_keeps_exact_unicode_spelling():
    from openkb.agent.source_excerpts import SourceExcerpts

    original = "<table><tr><td>Recovery:<br/>/home/cafe\u0301/&lt;node&gt;/state</td></tr></table>"
    content = "Inspect `/home/cafe\u0301/<node>/state`."
    assert SourceExcerpts([original]).render(content) == content


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["content", "description"])
@pytest.mark.parametrize(
    "original,path,wrong",
    [
        (
            "Inspect /system/config/cluster/<节点>/state/ on the recovery node.",
            "/system/config/cluster/<节点>/state/",
            "/systen/config/cluster/<节点>/state/",
        ),
        (
            r"Inspect C:\Program Files\Cluster\state.ini on the recovery node.",
            r"C:\Program Files\Cluster\state.ini",
            r"C:\Program Flies\Cluster\state.ini",
        ),
        (
            "<table><tr><td>/system/config/&lt;节点&gt;/state/</td></tr></table>",
            "/system/config/<节点>/state/",
            "/systen/config/<节点>/state/",
        ),
    ],
)
async def test_long_summary_repairs_changed_inline_path_before_publication(
    kb_dir, monkeypatch, field, original, path, wrong
):
    from openkb.agent.compiler import compile_long_doc

    wiki = kb_dir / "wiki"
    (wiki / "sources/manual.md").write_text(original)
    summary = wiki / "summaries/manual.md"
    generations = []

    def provider(**kwargs):
        messages = kwargs["messages"]
        if "Based on the summary above" in str(messages):
            raw = "{}"
        else:
            generations.append(messages)
            chosen = wrong if len(generations) == 1 else path
            if len(generations) == 2:
                assert "page_literal_conflict" in str(messages)
            window = json.loads(messages[1]["content"].split("\n", 1)[1])
            content = f"The recovery directory is `{chosen}`." if field == "content" else original
            raw = json.dumps(
                {
                    "description": f"Read `{chosen}`." if field == "description" else "Recovery",
                    "sections": [
                        {"id": section["id"], "content": content}
                        for section in window["output_sections"]
                    ],
                }
            )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=raw), finish_reason="stop")],
            usage=None,
        )

    async def asynchronous(**kwargs):
        return provider(**kwargs)

    monkeypatch.setattr("litellm.completion", provider)
    monkeypatch.setattr("litellm.acompletion", asynchronous)
    await compile_long_doc("manual", summary, "retained", kb_dir, "test-model", navigation=[])

    assert len(generations) == 2
    assert path in summary.read_text() and wrong not in summary.read_text()
    assert (wiki / "sources/manual.md").read_text() == original


@pytest.mark.asyncio
async def test_repeated_path_corruption_keeps_the_previous_summary(kb_dir, monkeypatch):
    from openkb.agent.compiler import compile_long_doc
    from openkb.compilation_report import CompilationIncomplete

    wiki = kb_dir / "wiki"
    (wiki / "sources/manual.md").write_text("Inspect /system/config/state on the recovery node.")
    summary = wiki / "summaries/manual.md"
    summary.write_text("Previous successful knowledge.\n")
    old = summary.read_bytes()
    generations = []

    def provider(**kwargs):
        if "Based on the summary above" in str(kwargs["messages"]):
            raw = "{}"
        else:
            generations.append(kwargs["messages"])
            window = json.loads(kwargs["messages"][1]["content"].split("\n", 1)[1])
            raw = json.dumps(
                {
                    "description": "Recovery",
                    "sections": [
                        {"id": section["id"], "content": "Read `/systen/config/state`."}
                        for section in window["output_sections"]
                    ],
                }
            )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=raw), finish_reason="stop")],
            usage=None,
        )

    async def asynchronous(**kwargs):
        return provider(**kwargs)

    monkeypatch.setattr("litellm.completion", provider)
    monkeypatch.setattr("litellm.acompletion", asynchronous)
    with pytest.raises(CompilationIncomplete):
        await compile_long_doc("manual", summary, "retained", kb_dir, "test-model", navigation=[])

    assert len(generations) == 2
    assert summary.read_bytes() == old


@pytest.mark.asyncio
@pytest.mark.parametrize("echo_description", [False, True])
async def test_update_repairs_only_new_path_literals_through_the_sdk(
    kb_dir, model_service, echo_description
):
    from test_vendor_sdk import response

    from openkb.agent.page_updates import PageUpdate
    from openkb.agent.source_windows import CompileSourceWindows
    from openkb.agent.update_generation import generate_page_update
    from openkb.application.execution import ExecutionContext
    from openkb.locks import kb_ingest_lock

    (kb_dir / ".env").write_text(f"LLM_API_KEY=fixture\nOPENAI_API_BASE={model_service.url}\n")
    (kb_dir / ".openkb/config.yaml").write_text(
        "model: openai/gpt-4o-mini\nlanguage: en\nmodel_policy:\n"
        "  retries: 0\n  max_calls: 2\n  compile_repair_calls: 1\n"
    )
    wiki = kb_dir / "wiki"
    source = "During recovery inspect /system/config/state.\n"
    (wiki / "sources/manual.md").write_text(source)
    page = wiki / "concepts/rules.md"
    description = "Read `/legacy/unknown`."
    old = (
        f"---\ndescription: {description}\n---\n\n"
        "Preserved legacy path `/legacy/unknown`.\n\nOld recovery rule.\n"
    )
    page.write_text(old)
    windows = CompileSourceWindows(wiki, "manual", kb_dir=kb_dir)
    originals = windows.prepare(
        {"name": "rules", "reading_locations": {"chars": f"0:{len(source)}"}},
        "concepts/rules.md",
        long=True,
    )
    patch = PageUpdate(old, originals=[source])
    for path in ("/systen/config/state", "/system/config/state"):
        model_service.replies.append(
            (
                200,
                response(
                    {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "base_sha256": patch.digest,
                                **({"description": description} if echo_description else {}),
                                "edits": [
                                    {
                                        "block_id": "b2",
                                        "operation": "replace",
                                        "content": f"During recovery inspect `{path}`.",
                                    }
                                ],
                            }
                        ),
                    }
                ),
            )
        )
    context = ExecutionContext()
    with kb_ingest_lock(kb_dir / ".openkb"), context.begin(kb_dir):
        result = await generate_page_update(
            patch=patch,
            originals=originals,
            windows=windows,
            page_path=page,
            model="ignored",
            bundle=None,
            system_message={"role": "system", "content": "Compile"},
            current_original=None,
            known_targets_message={"role": "user", "content": "rules"},
            task_message={"role": "user", "content": "Update recovery instructions."},
            fits=lambda messages: True,
        )
    assert len(model_service.requests) == 2
    assert "page_literal_conflict" in json.dumps(model_service.requests[1])
    assert "`/system/config/state`" in result["content"]
    assert "`/legacy/unknown`" in result["content"]
    assert result["description"] == description
    assert page.read_text() == old
