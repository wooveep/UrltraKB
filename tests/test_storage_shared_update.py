"""Large shared-page updates retain complete originals and accepted bounded work."""

import json

import pytest
from stream_transport import StreamTransport
from test_storage_compiler import compile_input

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.recompilation import recompile_document
from ultrakb.application.views import storage_scope
from ultrakb.config import save_config
from ultrakb.processing_policy import classify_markdown_tokens
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.knowledge_store import KnowledgeStore

pytestmark = pytest.mark.integration


def shared_update_input(tmp_path):
    root, identity, old = compile_input(tmp_path)
    paragraphs = [
        f"Paragraph {n}: old value 17. " + "Conditions remain applicable. " * 50 for n in range(24)
    ]
    body = "\n\n".join(paragraphs) + "\n"
    store = KnowledgeStore(root, identity)
    store.publish(
        store.prepare_revision(
            {
                "index.md": b"[[concepts/value]]",
                "summaries/fixture.md": (
                    b"---\nfull_text: sources/fixture.md\n---\nThe value is 17.\n"
                ),
                "concepts/value.md": (
                    "---\ndescription: Recorded value\nsources: [summaries/fixture.md]\n---\n"
                    "# Value\n\n" + body
                ).encode(),
            },
            expected=store.bind().publication_id,
            unit_revisions=(old.unit_revision_id,),
            generated_paths=("summaries/fixture.md", "concepts/value.md"),
        )
    )
    catalog = CatalogStore(root, identity)
    source = tmp_path / "fixture.txt"
    source.write_text("The value is 23.")
    revision = catalog.admit(source, source_key="fixture", expected=old.source_revision_id)
    processing = catalog.retain_processing(
        revision.revision_id,
        {"fixture.md": source},
        body_path="fixture.md",
        fingerprint="updated-text",
        operation_id="updated-text",
        decision=classify_markdown_tokens(5),
    )
    target = catalog.plan_unit(processing.processing_id, key="body", doc_name="fixture")
    source.unlink()
    save_config(
        root / ".ultrakb/config.yaml",
        {
            "model": "openai/gpt-4o-mini",
            "language": "en",
            "model_policy": {"max_calls": 128, "retries": 0},
            "model_capacity": {"max_input_bytes": 24000},
        },
    )
    return root, identity, target, paragraphs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "interrupt", [0, 1, 2], ids=["normal", "first-accepted", "second-accepted"]
)
@pytest.mark.parametrize("reconcile", [False, True], ids=["independent-groups", "bounded-conflict"])
async def test_large_shared_page_continues_accepted_groups_without_replacing_unrelated_text(
    tmp_path, monkeypatch, interrupt, reconcile
):
    from ultrakb.storage.checkpoint_store import CompileWork

    root, identity, target, paragraphs = shared_update_input(tmp_path)
    updates, reconciliations = [], []

    def respond(request, index):
        update = next(
            (
                message["content"]
                for message in request["messages"]
                if isinstance(message.get("content"), str)
                and message["content"].startswith("Temporary editable page blocks:\n")
            ),
            None,
        )
        if update:
            value, _ = json.JSONDecoder().raw_decode(update.split("\n", 1)[1])
            updates.append(value)
            bounded = any(
                "Bounded update reconciliation:" in str(message.get("content"))
                for message in request["messages"]
            )
            if bounded:
                reconciliations.append(request)
                return {
                    "content": json.dumps(
                        {
                            "status": "updated",
                            "base_sha256": value["base_sha256"],
                            "edits": [],
                            "description": "Value with all original conditions.",
                        }
                    )
                }
            return {
                "content": json.dumps(
                    {
                        "status": "updated",
                        "base_sha256": value["base_sha256"],
                        "edits": [
                            {
                                "block_id": block["id"],
                                "operation": "replace",
                                "content": block["text"].replace("old value 17.", "new value 23."),
                            }
                            for block in value["blocks"]
                            if "old value 17." in block["text"]
                        ],
                        **(
                            {"description": f"Local description {len(updates)}."}
                            if reconcile
                            else {}
                        ),
                    }
                )
            }
        if index == 1:
            return {
                "content": json.dumps(
                    {
                        "concepts": {
                            "create": [],
                            "update": [{"name": "value", "title": "Value"}],
                            "related": [],
                        },
                        "entities": [],
                    }
                )
            }
        return {"content": json.dumps({"description": "Fixture", "content": "The value is 23."})}

    original = CompileWork.save
    saved = 0

    def interrupted(self, key, *args, **options):
        nonlocal saved

        result = original(self, key, *args, **options)
        if key.startswith("page:") and (
            ":batch:" in key
            if not reconcile
            else ":conflict:" in key and not key.endswith(":bounded-v1")
        ):
            saved += 1
            if saved == interrupt:
                raise OSError("Accepted page step acknowledgement interrupted")
        return result

    scope, context = storage_scope(root, identity), ExecutionContext()
    with StreamTransport(respond) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        if interrupt:
            with monkeypatch.context() as failure:
                failure.setattr(CompileWork, "save", interrupted)
                failed = await recompile_document(
                    root, target.source_id, unit_id=target.unit_id, scope=scope, context=context
                )
            assert failed.status == "failed", (failed.message, failed.quality)
            assert storage_scope(root, identity).publication_id == scope.publication_id
        sent = list(service.requests)
        result = await recompile_document(
            root, target.source_id, unit_id=target.unit_id, scope=scope, context=context
        )
        assert result.status == "compiled", (result.message, result.quality, result.unfinished)
        assert len(updates) > 1
        assert bool(reconciliations) is reconcile
        assert all(request not in sent for request in service.requests[len(sent) :])
    content = KnowledgeStore(root, identity).bind().read("concepts/value.md").decode()
    assert all(
        paragraph.replace("old value 17.", "new value 23.") in content for paragraph in paragraphs
    )
    assert "old value 17." not in content
    assert "concept_generation_incomplete" not in result.quality
    if reconcile:
        from ultrakb.frontmatter import parse

        assert parse(content)["description"] == "Value with all original conditions."
