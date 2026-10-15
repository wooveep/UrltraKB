"""Compilation expands supplied originals instead of trusting rewritten syntax."""

import json

import pytest


def test_original_excerpt_preparation_honors_cancellation_before_model_calls():
    from ultrakb.agent.compiler import short_document_messages
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings
    from ultrakb.locks import LockCancelled

    executor = CompletionExecutor(
        RoleBindings("deepseek/deepseek-flash", LlmCredentialBundle()),
        ModelCallPolicy(max_calls=0, retries=0, cancelled=lambda: True),
    )
    original = "```text\nsetting=KEEP\n```\n\n" * 2
    with executor.activate(), pytest.raises(LockCancelled):
        short_document_messages("manual", original, "", "en")
    assert executor.calls == 0


def test_reused_excerpt_keeps_the_original_conditions_and_each_visible_copy():
    from ultrakb.agent.source_excerpts import SourceExcerpts

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
    from ultrakb.agent.page_response import PageResponseError

    with pytest.raises(PageResponseError, match="Unknown original excerpt ID"):
        excerpts.render("[[original:x1.4]]")


@pytest.mark.parametrize("second", ["setting=KEEP \n\n", "setting=KEEP\r\n\r\n"])
def test_excerpt_identity_does_not_merge_different_original_whitespace(second):
    from ultrakb.agent.source_excerpts import SourceExcerpts

    excerpts = SourceExcerpts(["setting=KEEP\n\n" + second])
    catalog = json.loads(excerpts.message()["content"].split("\n", 1)[1])
    assert [b["id"] for b in catalog["blocks"]] == ["x1.1", "x1.2"]


@pytest.mark.asyncio
async def test_summary_repairs_changed_configuration_with_exact_original_block(
    kb_dir, monkeypatch, live_llm
):
    from ultrakb.agent.compiler import _llm_call_page_async, _write_summary
    from ultrakb.agent.document_summary import SYSTEM_TEMPLATE, summary_messages, window_sections
    from ultrakb.agent.page_response import parse_summary
    from ultrakb.agent.source_excerpts import SourceExcerpts
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.locks import atomic_write_text

    wiki = kb_dir / "wiki"
    original = "# Compute template\n\nFor X86 hosts only:\n@^compute\n%end\n"
    atomic_write_text(wiki / "sources/manual.md", original)
    reader = CompileSourceWindows(wiki, "manual")
    window = next(reader.iter_document_windows(fits=lambda _: True))
    sections = tuple(s["id"] for s in window_sections(window))
    invalid = json.dumps(
        {
            "description": "Compute template",
            "sections": [
                {"id": section, "content": "For X86 hosts only:\n\n```text\n# @^compute\n%end\n```"}
                for section in sections
            ],
        }
    )
    messages = summary_messages(
        {"role": "system", "content": SYSTEM_TEMPLATE.format(language="en")},
        "manual",
        window,
    )
    live_llm.install(monkeypatch)
    before = len(live_llm.calls)
    received = await _llm_call_page_async(
        "deepseek/deepseek-flash",
        messages,
        "document-summary:1",
        bundle=live_llm.credentials,
        initial_response=invalid,
        response_protocol="summary-json",
        expected_sections=sections,
        response_format={"type": "json_object"},
        max_tokens=512,
        original_excerpts=SourceExcerpts([window]),
        numeric_sources=[original],
    )
    result = parse_summary(received, sections)
    _write_summary(
        wiki,
        "manual",
        "\n\n".join(s["content"] for s in result["sections"]),
        description=result["description"],
        full_text="sources/manual.md",
    )
    body = (wiki / "summaries/manual.md").read_text()
    assert len(live_llm.calls) - before == 1
    assert "page_literal_conflict" in str(live_llm.calls[-1]["messages"])
    assert "For X86 hosts only:\n@^compute\n%end\n" in body
    assert "# @^compute" not in body and "[[original:" not in body


def test_update_proposal_expands_original_before_acceptance_and_keeps_old_code(kb_dir):
    from publication_inputs import update_inputs

    from ultrakb.agent.page_delivery import validate_page_delivery
    from ultrakb.agent.page_response import PageResponseError

    source = '    set\n    "WLIC"\nDuring recovery use "retained-value"\n'
    old = "# Rules\n\n```text\nlegacy=KEEP\n```\n\nOld recovery rule.\n"
    page, reader, originals, patch, _, _ = update_inputs(kb_dir, old, source)
    excerpts = reader.original_excerpts(originals, long=True)
    invalid = json.dumps(
        {
            "base_sha256": patch.digest,
            "edits": [
                {"block_id": "b3", "operation": "replace", "content": '```text\nset\n"WLIC"\n```'},
            ],
        }
    )
    with pytest.raises(PageResponseError, match="page_literal_conflict"):
        validate_page_delivery(
            invalid, page_update=patch, read_current=page.read_text, original_excerpts=excerpts
        )
    valid = json.dumps(
        {
            "base_sha256": patch.digest,
            "edits": [
                {"block_id": "b3", "operation": "replace", "content": "[[original:x1.1]]"},
            ],
        }
    )
    result = json.loads(
        validate_page_delivery(
            valid,
            page_update=patch,
            read_current=page.read_text,
            original_excerpts=excerpts,
        )
    )
    assert source in result["content"]
    assert "```text\nlegacy=KEEP\n```" in result["content"]
    assert page.read_text() == old


@pytest.mark.parametrize("operation", ["replace", "insert_before", "insert_after"])
@pytest.mark.parametrize("initial", ["hidden", "unterminated", "literal"])
def test_update_keeps_literal_syntax_visible_and_separate_from_existing_prose(
    kb_dir, operation, initial
):
    from publication_inputs import update_inputs

    from ultrakb.agent.page_delivery import validate_page_delivery
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.content_blocks import markdown_layout

    source = 'if [[ -n "$VALUE" ]]; then\n    run_check\nfi\n'
    old = "Existing unrelated prose.\n"
    page, reader, originals, patch, _, _ = update_inputs(kb_dir, old, source)
    excerpts = reader.original_excerpts(originals, long=True)
    initial_content = {
        "hidden": "<!--\n[[original:x1.1]]\n-->",
        "unterminated": f"```sh\n{source}",
        "literal": f"```sh\n{source}```",
    }[initial]

    def input_data(content):
        return json.dumps(
            {
                "base_sha256": patch.digest,
                "edits": [
                    {"block_id": "b1", "operation": operation, "content": content},
                ],
            }
        )

    if initial != "literal":
        with pytest.raises(PageResponseError):
            validate_page_delivery(
                input_data(initial_content),
                page_update=patch,
                read_current=page.read_text,
                original_excerpts=excerpts,
            )
        initial_content = "[[original:x1.1]]"
    result = json.loads(
        validate_page_delivery(
            input_data(initial_content),
            page_update=patch,
            read_current=page.read_text,
            original_excerpts=excerpts,
        )
    )
    tokens, _ = markdown_layout(result["content"])
    assert [token.content for token in tokens if token.type == "fence"] == [source]
    if operation != "replace":
        assert any(token.type == "inline" and token.content == old.strip() for token in tokens)
    assert page.read_text() == old


@pytest.mark.parametrize(
    "content",
    [
        "[[original:x1.1]]",
        "# Rule\n\n[[original:x1.1]]\n\nRetain this condition.",
        "# Rule\n\n```text\nsetting=KEEP\n```\n\n[[original:x1.1]]",
    ],
)
def test_excerpt_expansion_is_idempotent_after_checkpoint_restoration(content):
    from ultrakb.agent.source_excerpts import SourceExcerpts

    excerpts = SourceExcerpts(["setting=KEEP\n"])
    first = excerpts.render(content)
    restored = excerpts.render(first)
    assert restored == first
    assert "setting=KEEP" in restored and "[[original:" not in restored
