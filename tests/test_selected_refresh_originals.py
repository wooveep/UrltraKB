"""Selected refresh keeps authenticated originals and their retained references."""

import asyncio
import json

import pymupdf
import pytest
from test_selected_refresh_sdk import _editing_request, _import, _prepared, _reply
from test_vendor_sdk import response

pytest_plugins = ("test_vendor_sdk",)


def _unchanged(request):
    edit = _editing_request(request)
    return response(
        {
            "role": "assistant",
            "content": json.dumps(
                {
                    "status": "unchanged",
                    "base_sha256": edit["base_sha256"],
                    "reason": "No additional shared contribution is required.",
                }
            ),
        }
    )


def test_selected_refresh_rejects_an_unregistered_preferred_original(kb_dir, model_service):
    from openkb.agent.source_windows import CompileSourceWindows
    from openkb.application.documents import import_document
    from openkb.application.refresh import refresh_selected_sources, refresh_status
    from openkb.application.views import view_scope

    (kb_dir / ".env").write_text(f"LLM_API_KEY=fixture\nOPENAI_API_BASE={model_service.url}\n")
    (kb_dir / ".openkb/config.yaml").write_text(
        "model: openai/gpt-4o-mini\nlanguage: en\nmodel_policy:\n  retries: 0\n  max_calls: 20\n"
    )
    path = kb_dir / "problem.pdf"

    def write_pdf(text):
        with pymupdf.open() as document:
            document.new_page().insert_text((72, 72), text)
            document.save(path)

    write_pdf("Original problem policy allows direct access.")
    _reply(
        model_service,
        {
            "description": "Problem policy",
            "content": "Original problem policy allows direct access.",
        },
    )
    _reply(model_service, {"concepts": {"create": [{"name": "shared", "title": "Shared"}]}})
    _reply(
        model_service,
        {
            "description": "Shared policy",
            "content": "# Shared\n\nOriginal problem policy allows direct access.",
        },
    )
    first = import_document(kb_dir, path)
    assert first.status == "added", first.message
    _import(kb_dir, model_service, "normal", "UNRELATED NORMAL ORIGINAL SENTINEL.")
    write_pdf("Corrected problem policy requires approval.")
    _reply(
        model_service,
        {"description": "Problem policy", "content": "Corrected problem policy requires approval."},
    )
    _reply(model_service, {})
    updated = import_document(kb_dir, path)
    assert updated.status == "added", updated.message
    scope = view_scope(kb_dir, first.units[0].view_id)
    assert (scope.wiki_dir / "sources/problem.json").is_file()
    normal_summary = scope.wiki_dir / "summaries/normal.md"
    normal_before = normal_summary.read_bytes()

    # A stale auxiliary file is valid text but is not part of the PDF's normalization.
    shadow = scope.wiki_dir / "sources/problem.content.json"
    shadow.write_bytes((scope.wiki_dir / "sources/normal.content.json").read_bytes())
    assert (
        CompileSourceWindows(scope.wiki_dir, "problem").original_path
        == "sources/problem.content.json"
    )
    before = refresh_status(kb_dir, scope=scope)["needs_refresh"]
    count = len(model_service.requests)
    model_service.replies.append((200, _unchanged))

    result = asyncio.run(refresh_selected_sources(kb_dir, [first.source_id], scope=scope))

    after = refresh_status(kb_dir, scope=scope)["needs_refresh"]
    assert result.status in {"partial", "failed"}, result.message
    assert after["concepts/shared.md"] == before["concepts/shared.md"]
    assert len(model_service.requests) == count
    assert normal_summary.read_bytes() == normal_before


@pytest.mark.parametrize("all_sources", [False, True], ids=["partial", "complete"])
def test_selected_publication_retains_originals_of_old_page_dependencies(
    kb_dir, model_service, all_sources
):
    from openkb.application.refresh import refresh_selected_sources, refresh_status
    from openkb.knowledge_evidence import validated_input
    from openkb.unit_publication import read_head

    sources, scope = _prepared(kb_dir, model_service)
    count = len(model_service.requests)
    if all_sources:
        model_service.replies.append((200, _unchanged))

    result = asyncio.run(
        refresh_selected_sources(kb_dir, sources if all_sources else sources[:1], scope=scope)
    )

    assert result.status == ("completed" if all_sources else "partial"), result.message
    assert len(model_service.requests) - count == int(all_sources)
    head = read_head(kb_dir, scope.view_id)
    manifest_path = (
        kb_dir
        / ".openkb/knowledge"
        / scope.view_id
        / "revisions"
        / head.knowledge_revision_id
        / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text())
    current = set(manifest["input_revisions"])
    dependencies = {
        identity for identities in manifest["page_dependencies"].values() for identity in identities
    }
    assert dependencies - current, "The fixture must retain older page evidence."
    originals = set()
    for identity in current | dependencies:
        _, source = validated_input(kb_dir, identity, scope.view_id)
        originals.add(source.original)
        originals.update(asset.artifact for asset in source.assets if asset.artifact)
    assert originals <= set(manifest["original_references"])
    assert bool(refresh_status(kb_dir, scope=scope)["needs_refresh"]) is not all_sources
