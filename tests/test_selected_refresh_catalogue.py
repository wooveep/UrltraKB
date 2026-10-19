"""Selected refresh keeps one catalogue while earlier pages change their titles."""

import asyncio
import json

from publication_fixtures import publish_document
from stream_transport import StreamTransport

from ultrakb.config import LlmCredentialBundle


def test_selected_refresh_resolves_later_links_against_the_original_operation_catalogue(
    kb_dir, monkeypatch
):
    from ultrakb.application.pages import read_page
    from ultrakb.application.refresh import refresh_selected_sources, refresh_status
    from ultrakb.application.views import view_scope

    (kb_dir / ".ultrakb/config.yaml").write_text(
        "model: openai/gpt-4o-mini\nlanguage: en\nmodel_capacity:\n  max_input_bytes: 100000\n"
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

    def complete(payload, index):
        if "Resolve one wiki link identity" in str(payload["messages"][0]["content"]):
            data = json.loads(payload["messages"][1]["content"].partition("\n")[2])
            selected = next(
                (item["path"] for item in data["candidates"] if "Original Identity" in str(item)),
                None,
            )
            decision = {"id": data["id"], "path": selected}
            if "status=matched" in str(payload["messages"][0]["content"]):
                decision = {
                    "id": data["id"],
                    "status": "matched" if selected else "no_match",
                    "paths": [selected] if selected else [],
                }
            return {"content": json.dumps({"resolutions": [decision]})}
        request = next(
            (
                message["content"].partition("\n")[2]
                for message in payload["messages"]
                if isinstance(message["content"], str)
                and message["content"].startswith("Temporary editable page blocks:\n")
            ),
            None,
        )
        if request is None:
            return {"content": "{}"}
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
        return {
            "content": json.dumps(
                {
                    "status": "updated",
                    "base_sha256": patch["base_sha256"],
                    "edits": edits,
                }
            )
        }

    with StreamTransport(complete) as service:
        (kb_dir / ".env").write_text(f"LLM_API_KEY=local-test-key\nOPENAI_API_BASE={service.url}\n")
        result = asyncio.run(refresh_selected_sources(kb_dir, [first.source_id], scope=scope))
    scope = view_scope(kb_dir, first.units[0].view_id)
    assert generated == ["policy", "reference"]
    assert len(service.requests) == 2, "Both updates resolve against the original catalogue"
    assert result.status in {"completed", "partial"}, result.message
    assert "# Renamed Identity" in read_page(kb_dir, "concepts/a-policy", scope=scope).body
    assert (
        "[[concepts/a-policy|the policy]]"
        in read_page(kb_dir, "concepts/b-reference", scope=scope).body
    )
