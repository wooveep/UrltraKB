"""Full planning and page requests must fit before reaching the real SDK transport."""

import json

import pytest
from test_vendor_sdk import response

pytest_plugins = ("test_vendor_sdk",)


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized", ["navigation", "new_page"])
async def test_complete_compile_input_never_sends_over_known_capacity(
    kb_dir, model_service, oversized
):
    from openkb.agent.compiler import compile_long_doc
    from openkb.application.execution import ExecutionContext
    from openkb.compilation_report import CompilationIncomplete
    from openkb.locks import kb_ingest_lock
    from openkb.model_tokenizer import ContextCapacityError

    (kb_dir / ".env").write_text(f"LLM_API_KEY=fixture\nOPENAI_API_BASE={model_service.url}\n")
    (kb_dir / ".openkb/config.yaml").write_text(
        "model: openai/gpt-4o-mini\nlanguage: en\nmodel_capacity:\n"
        "  max_input_tokens: 5000\n  tokenizer_model: gpt-4o-mini\n"
        "model_policy:\n  max_calls: 20\n  retries: 0\n"
    )
    original = "Operating context must be reviewed.\n\n" * (2000 if oversized == "new_page" else 1)
    (kb_dir / "wiki/sources/manual.md").write_text(original)
    summary = kb_dir / "wiki/summaries/manual.md"
    summary.write_text("---\nfull_text: sources/manual.md\n---\nPrevious result.")
    navigation = [
        {"node_id": str(i), "title": f"Operating section {i}", "start_index": 1, "end_index": 1}
        for i in range(1000 if oversized == "navigation" else 0)
    ]

    def reply(request):
        messages = request["messages"]
        if "Based on the summary above" in str(messages):
            result = (
                {
                    "concepts": {
                        "create": [
                            {
                                "name": "conditions",
                                "reading_locations": {"chars": f"0:{len(original)}"},
                            }
                        ]
                    }
                }
                if oversized == "new_page"
                else {}
            )
        elif messages[1]["content"].startswith("Original document:"):
            batch = json.loads(messages[1]["content"].split("\n", 1)[1])
            result = {
                "description": "Conditions",
                "sections": [
                    {"id": s["id"], "content": "Review the operating context."}
                    for s in batch["output_sections"]
                ],
            }
        else:
            result = {
                "description": "Conditions",
                "content": "# Conditions\n\nReview the operating context.",
            }
        return response({"role": "assistant", "content": json.dumps(result)})

    model_service.replies.extend([(200, reply)] * 20)
    context = ExecutionContext()
    with kb_ingest_lock(kb_dir / ".openkb"), context.begin(kb_dir):
        with pytest.raises((ContextCapacityError, CompilationIncomplete)):
            await compile_long_doc(
                "manual", summary, "id", kb_dir, "ignored", navigation=navigation
            )
    assert model_service.requests  # Earlier fitting summary requests were real sends.
    if oversized == "navigation":
        assert not any("Generated navigation" in str(r) for r in model_service.requests)
    else:
        assert any("Generated navigation" in str(r) for r in model_service.requests)
        assert not any("Create a concept page" in str(r) for r in model_service.requests)
    import tiktoken

    tokenizer = tiktoken.encoding_for_model("gpt-4o-mini")
    assert all(
        # Model routing and streaming flags are transport metadata, not input.
        len(
            tokenizer.encode(
                json.dumps(
                    {k: v for k, v in r.items() if k not in {"model", "stream"}}, ensure_ascii=False
                )
            )
        )
        <= 5000
        for r in model_service.requests
    )
