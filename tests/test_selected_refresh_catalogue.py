"""Selected refresh keeps one catalogue while earlier pages change their titles."""

import asyncio
import json

from litellm import ModelResponse
from publication_fixtures import publish_document

from ultrakb.config import LlmCredentialBundle


def test_selected_refresh_resolves_later_links_against_the_original_operation_catalogue(
    kb_dir, monkeypatch
):
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.pages import read_page
    from ultrakb.application.refresh import refresh_selected_sources, refresh_status
    from ultrakb.application.views import view_scope
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings

    (kb_dir / ".ultrakb/config.yaml").write_text(
        "model: deepseek/deepseek-flash\nlanguage: en\nmodel_capacity:\n  max_input_bytes: 30000\n"
    )
    bundle = LlmCredentialBundle(api_key="local-test-key")
    source = kb_dir / "policy.md"
    source.write_text("Original Identity permits direct use.")
    first = publish_document(
        kb_dir,
        source,
        source.read_text(),
        bundle=bundle,
        concept_pages={
            "a-policy": "# Original Identity\n\nThe original policy permits direct use.\n",
            "b-reference": "# Policy reference\n\nThe old policy reference.\n",
        },
    )
    source.write_text(
        "Renamed Identity requires inspection. Original Identity was its previous name."
    )
    publish_document(kb_dir, source, source.read_text(), bundle=bundle)
    scope = view_scope(kb_dir, first.units[0].view_id)
    assert {"concepts/a-policy.md", "concepts/b-reference.md"} <= set(
        refresh_status(kb_dir, scope=scope)["needs_refresh"]
    )
    generated = []

    async def complete(**kwargs):
        request = next(
            message["content"].partition("\n")[2]
            for message in kwargs["messages"]
            if isinstance(message["content"], str)
            and message["content"].startswith("Temporary editable page blocks:\n")
        )
        patch = json.JSONDecoder().raw_decode(request)[0]
        first_page = any("# Original Identity" in block["text"] for block in patch["blocks"])
        generated.append("policy" if first_page else "reference")
        edits = (
            [
                {"block_id": "b1", "operation": "replace", "content": "# Renamed Identity\n"},
                {
                    "block_id": "b2",
                    "operation": "replace",
                    "content": "\nInspection is required.\n",
                },
            ]
            if first_page
            else [
                {
                    "block_id": "b2",
                    "operation": "replace",
                    "content": "\nInspect [[Original Identity|the policy]].\n",
                }
            ]
        )
        return ModelResponse(
            model="unused",
            choices=[
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "status": "updated",
                                "base_sha256": patch["base_sha256"],
                                "edits": edits,
                            }
                        ),
                    },
                }
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        )

    monkeypatch.setattr("litellm.acompletion", complete)
    context = ExecutionContext(
        executor=CompletionExecutor(
            RoleBindings("unused", bundle), ModelCallPolicy(max_calls=10, retries=0)
        )
    )
    result = asyncio.run(
        refresh_selected_sources(kb_dir, [first.source_id], scope=scope, context=context)
    )
    assert result.status == "completed", result.message
    assert generated == ["policy", "reference"]
    assert "# Renamed Identity" in read_page(kb_dir, "concepts/a-policy", scope=scope).body
    assert (
        "[[concepts/a-policy|the policy]]"
        in read_page(kb_dir, "concepts/b-reference", scope=scope).body
    )
