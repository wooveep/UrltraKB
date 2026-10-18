"""Synthetic protocol replies exercise real application orchestration/publication.

This checks shared-path behavior and evidence conservation, not model quality.
"""

import asyncio
import json

import pytest

from ultrakb.agent import compiler, update_generation
from ultrakb.config import LlmCredentialBundle
from ultrakb.locks import atomic_write_text, kb_ingest_lock


@pytest.mark.parametrize("operation", ["import", "reprocess", "refresh"])
def test_application_paths_share_bounded_coordination_and_safe_patch_assembly(
    kb_dir, monkeypatch, operation
):
    from publication_fixtures import publish_document
    from test_reconciliation_recovery import request_scope

    from ultrakb.application.documents import import_document
    from ultrakb.application.refresh import refresh_selected_sources, refresh_status
    from ultrakb.application.reprocessing import preview_reprocessing, reprocess_source
    from ultrakb.application.views import view_scope
    from ultrakb.unit_publication import read_head

    bundle = LlmCredentialBundle(api_key="offline-protocol-fixture")
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        atomic_write_text(
            kb_dir / ".ultrakb/config.yaml",
            "model: deepseek/deepseek-flash\n"
            "language: en\nmodel_policy:\n  max_calls: 100\n  retries: 0\n"
            "model_capacity:\n  max_input_bytes: 100000\n",
        )
    winter = "WINTER_RULE Close the inlet before inspection. " * 50
    summer = "SUMMER_RULE Run the maintenance command. " * 50
    body = "# Shared\n\nOld winter.\n\nOld summer.\n"
    path = kb_dir / "manual.md"
    path.write_text(winter + "\n\n" + summer if operation == "reprocess" else winter)
    if operation == "reprocess":
        other = kb_dir / "other.md"
        other.write_text(winter)
        publish_document(kb_dir, other, winter, bundle=bundle, shared_body=body)
    first = publish_document(
        kb_dir,
        path,
        path.read_text(),
        bundle=bundle,
        shared_body=None if operation == "reprocess" else body,
    )
    if operation == "refresh":
        second_path = kb_dir / "summer.md"
        second_path.write_text(summer)
        second = publish_document(
            kb_dir,
            second_path,
            summer,
            bundle=bundle,
            shared_body=body,
            shared_sources=["summaries/manual.md", "summaries/summer.md"],
        )
        path.write_text(winter + "Updated winter scope.\n")
        publish_document(kb_dir, path, path.read_text(), bundle=bundle)
        second_path.write_text(summer + "Updated summer scope.\n")
        publish_document(kb_dir, second_path, second_path.read_text(), bundle=bundle)

    scope = view_scope(kb_dir, first.units[0].view_id)
    before = read_head(kb_dir)
    calls, bounded_targets = [], set()

    def fits(messages):
        return (
            len(json.dumps(messages, ensure_ascii=False).encode()) <= 40000
            and sum(len(window["content"]) for window in request_scope(messages)[0]) <= 1200
        )

    monkeypatch.setattr(update_generation, "update_capacity", lambda *args: fits)

    def respond(model, messages, stage, **kwargs):
        calls.append(stage)
        if stage == "summary":
            return json.dumps(
                {"description": "Source rules", "content": "# Manual\n\nSource rules."}
            )
        if stage == "concepts-plan":
            return json.dumps(
                {
                    "concepts": {
                        "create": [],
                        "update": [
                            {"name": "shared", "brief": "Integrate both source conditions."}
                        ],
                        "related": [],
                    },
                    "entities": {"create": [], "update": [], "related": []},
                }
            )
        assert fits(messages)
        windows, blocks = request_scope(messages)
        page_request = next(
            m["content"].split("\n", 1)[1]
            for m in messages
            if isinstance(m.get("content"), str)
            and m["content"].startswith("Temporary editable page blocks:\n")
        )
        base = json.JSONDecoder().raw_decode(page_request)[0]["base_sha256"]
        text = " ".join(window["content"] for window in windows)
        marker = "Bounded update reconciliation:\n"
        control = next(
            (
                json.JSONDecoder().raw_decode(m["content"].split(marker, 1)[1])[0]
                for m in messages
                if isinstance(m.get("content"), str) and marker in m["content"]
            ),
            None,
        )
        if not control:
            text += " ".join(
                part.get("text", "")
                for message in messages
                if isinstance(message.get("content"), list)
                for part in message["content"]
                if part.get("text", "").startswith("Current original:")
            )
        metadata = control["metadata_target"] if control else True
        if control:
            bounded_targets.add(tuple(block["id"] for block in blocks))
        edits = []
        for token, block_id, replacement in (
            ("WINTER_RULE", "b2", "Updated paragraph."),
            ("SUMMER_RULE", "b3", "    command"),
        ):
            if token in text and any(b["id"] == block_id for b in blocks):
                edits.append({"block_id": block_id, "operation": "replace", "content": replacement})
        result = {"status": "updated", "base_sha256": base, "edits": edits}
        if metadata:
            result["description"] = "Winter and summer rules"
        elif not edits:
            result = {
                "status": "unchanged",
                "base_sha256": base,
                "reason": "Retain the previous source contribution.",
            }
        return json.dumps(result)

    async def arespond(*args, **kwargs):
        return respond(*args, **kwargs)

    monkeypatch.setattr(compiler, "_llm_call", respond)
    monkeypatch.setattr(compiler, "_llm_call_async", arespond)
    if operation == "import":
        incoming = kb_dir / "summer.md"
        incoming.write_text(summer)
        result = import_document(kb_dir, incoming)
    elif operation == "reprocess":
        preview = preview_reprocessing(kb_dir, first.source_id)
        result = reprocess_source(kb_dir, first.source_id, version=preview["version"])
    else:
        result = asyncio.run(
            refresh_selected_sources(
                kb_dir,
                [first.source_id, second.source_id],
                scope=scope,
                page_paths=["concepts/shared.md"],
            )
        )
    assert result.status in {"added", "completed", "partial"}, result.message
    after = read_head(kb_dir)
    assert after.knowledge_revision_id != before.knowledge_revision_id
    page = (scope.wiki_dir / "concepts/shared.md").read_text()
    assert "Updated paragraph.\n\n    command" in page
    assert "Winter and summer rules" in page
    assert () in bounded_targets, "Every application path must reach bounded metadata coordination"
    assert "concepts/shared.md" not in refresh_status(kb_dir, scope=scope)["needs_refresh"]
    count = len(calls)
    if operation == "import":
        assert import_document(kb_dir, incoming).status == "skipped"
    elif operation == "reprocess":
        assert (
            reprocess_source(kb_dir, first.source_id, version=preview["version"]).status
            == "skipped"
        )
    else:
        asyncio.run(
            refresh_selected_sources(
                kb_dir,
                [first.source_id, second.source_id],
                scope=scope,
                page_paths=["concepts/shared.md"],
            )
        )
    assert len(calls) == count, "Published work must not be regenerated on an ordinary repeat"
