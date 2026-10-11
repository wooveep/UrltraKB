"""Compilation expands supplied originals instead of trusting rewritten syntax."""

import json
from types import SimpleNamespace

import pytest

pytest_plugins = ("test_vendor_sdk",)


def test_original_excerpt_preparation_honors_cancellation_before_model_calls(model_service):
    from test_llm_execution import executor_for

    from openkb.agent.compiler import short_document_messages
    from openkb.locks import LockCancelled

    executor = executor_for(model_service, cancelled=lambda: True)
    original = "```text\nsetting=KEEP\n```\n\n" * 1000
    with executor.activate(), pytest.raises(LockCancelled):
        short_document_messages("manual", original, "", "en")
    assert model_service.requests == []


def test_reused_excerpt_keeps_the_original_conditions_and_each_visible_copy():
    from openkb.agent.source_excerpts import SourceExcerpts

    original = "X86 only.\n\nsetting=KEEP\n\nARM only.\n\nsetting=KEEP\n\n"
    excerpts = SourceExcerpts([original, "setting=KEEP\n\n"])
    messages = excerpts.messages([{"role": "user", "content": original}])
    assert messages[0]["content"] == original
    catalog = json.loads(messages[1]["content"].split("\n", 1)[1])
    commands = [b for b in catalog["blocks"] if b["opening"] == "setting=KEEP"]
    assert [(b["id"], b["source"], b["start"], b["end"]) for b in commands] == [
        ("x1.2", 1, 11, 25),
        ("x2.1", 2, 0, 14),
    ]
    rendered = excerpts.render("X86 only.\n\n[[original:x1.2]]\n\nARM only.\n\n[[original:x1.2]]")
    assert rendered.count("setting=KEEP\n") == 2
    assert "X86 only." in rendered and "ARM only." in rendered
    from openkb.agent.page_response import PageResponseError

    with pytest.raises(PageResponseError, match="Unknown original excerpt ID"):
        excerpts.render("[[original:x1.4]]")


@pytest.mark.parametrize("second", ["setting=KEEP \n\n", "setting=KEEP\r\n\r\n"])
def test_excerpt_identity_does_not_merge_different_original_whitespace(second):
    from openkb.agent.source_excerpts import SourceExcerpts

    excerpts = SourceExcerpts(["setting=KEEP\n\n" + second])
    catalog = json.loads(excerpts.message()["content"].split("\n", 1)[1])
    assert [b["id"] for b in catalog["blocks"]] == ["x1.1", "x1.2"]


@pytest.mark.asyncio
async def test_summary_repairs_changed_configuration_with_exact_original_block(kb_dir, monkeypatch):
    from openkb.agent.compiler import compile_long_doc

    wiki = kb_dir / "wiki"
    original = "# Compute template\n\nFor X86 hosts only:\n@^compute\n%end\n"
    (wiki / "sources/manual.md").write_text(original)
    summary = wiki / "summaries/manual.md"
    summary.write_text("---\nfull_text: sources/manual.md\n---\nOld knowledge.")
    generations = []

    def provider(**kwargs):
        messages = kwargs["messages"]
        if "Based on the summary above" in str(messages):
            raw = "{}"
        else:
            generations.append(messages)
            window = json.loads(messages[1]["content"].split("\n", 1)[1])
            if len(generations) == 1:
                content = "For X86 hosts only:\n\n```text\n# @^compute\n%end\n```"
            else:
                assert "page_literal_conflict" in str(messages)
                catalog = next(
                    json.loads(m["content"].split("\n", 1)[1])
                    for m in messages
                    if isinstance(m.get("content"), str)
                    and m["content"].startswith("Original excerpt catalog:\n")
                )
                block = next(b for b in catalog["blocks"] if "For X86 hosts" in b["opening"])
                content = f"[[original:{block['id']}]]"
            raw = json.dumps(
                {
                    "description": "Compute template",
                    "sections": [
                        {"id": s["id"], "content": content} for s in window["output_sections"]
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
    await compile_long_doc("manual", summary, "id", kb_dir, "test-model", navigation=[])

    body = summary.read_text()
    assert len(generations) == 2
    assert "For X86 hosts only:\n@^compute\n%end\n" in body
    assert "# @^compute" not in body and "[[original:" not in body


@pytest.mark.asyncio
async def test_update_proposal_expands_original_before_acceptance_and_keeps_old_code(
    kb_dir, model_service
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
    source = '    set\n    "WLIC"\nDuring recovery use "retained-value"\n'
    (wiki / "sources/manual.md").write_text(source)
    page = wiki / "concepts/rules.md"
    old = "# Rules\n\n```text\nlegacy=KEEP\n```\n\nOld recovery rule.\n"
    page.write_text(old)
    windows = CompileSourceWindows(wiki, "manual", kb_dir=kb_dir)
    originals = windows.prepare(
        {"name": "rules", "reading_locations": {"chars": f"0:{len(source)}"}},
        "concepts/rules.md",
        long=True,
    )
    patch = PageUpdate(old, originals=[source])
    for content in ('```text\nset\n"WLIC"\n```', "[[original:x1.1]]"):
        model_service.replies.append(
            (
                200,
                response(
                    {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "base_sha256": patch.digest,
                                "edits": [
                                    {"block_id": "b3", "operation": "replace", "content": content}
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
    assert source in result["content"]
    assert "```text\nlegacy=KEEP\n```" in result["content"]
    assert page.read_text() == old


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["replace", "insert_before", "insert_after"])
@pytest.mark.parametrize("initial", ["hidden", "unterminated", "literal"])
async def test_update_keeps_literal_syntax_visible_and_separate_from_existing_prose(
    kb_dir, model_service, operation, initial
):
    from test_vendor_sdk import response

    from openkb.agent.page_updates import PageUpdate
    from openkb.agent.source_windows import CompileSourceWindows
    from openkb.agent.update_generation import generate_page_update
    from openkb.application.execution import ExecutionContext
    from openkb.content_blocks import markdown_layout
    from openkb.locks import kb_ingest_lock

    (kb_dir / ".env").write_text(f"LLM_API_KEY=fixture\nOPENAI_API_BASE={model_service.url}\n")
    (kb_dir / ".openkb/config.yaml").write_text(
        "model: openai/gpt-4o-mini\nlanguage: en\nmodel_policy:\n"
        "  retries: 0\n  max_calls: 2\n  compile_repair_calls: 1\n"
    )
    wiki = kb_dir / "wiki"
    source = 'if [[ -n "$VALUE" ]]; then\n    run_check\nfi\n'
    (wiki / "sources/manual.md").write_text(source)
    page = wiki / "concepts/rules.md"
    old = "Old recovery instructions.\n"
    page.write_text(old)
    windows = CompileSourceWindows(wiki, "manual", kb_dir=kb_dir)
    originals = windows.prepare(
        {"name": "rules", "reading_locations": {"chars": f"0:{len(source)}"}},
        "concepts/rules.md",
        long=True,
    )
    patch = PageUpdate(old, known_targets=set(), originals=[source])
    first = {
        "hidden": "<!--\n[[original:x1.1]]\n-->",
        "unterminated": f"```sh\n{source}",
        "literal": f"```sh\n{source}```",
    }[initial]
    for content in (first, "[[original:x1.1]]"):
        model_service.replies.append(
            (
                200,
                response(
                    {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "base_sha256": patch.digest,
                                "edits": [
                                    {"block_id": "b1", "operation": operation, "content": content}
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
    tokens, _ = markdown_layout(result["content"])
    assert len(model_service.requests) == (1 if initial == "literal" else 2)
    assert [t.content for t in tokens if t.type == "fence"] == [source]
    if operation != "replace":
        assert any(t.type == "inline" and t.content == old.strip() for t in tokens)
    assert page.read_text() == old
