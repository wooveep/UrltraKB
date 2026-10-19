"""Generated files preserve logical storage paths and exact historical knowledge inputs."""

import os

import pytest
from stream_transport import StreamTransport
from test_storage_deck_generation import deck_knowledge, tool_response

from ultrakb.application.generators import GenerationOptions, generate_artifact, preview_generation
from ultrakb.application.pages import read_page
from ultrakb.application.views import storage_scope
from ultrakb.processing_policy import classify_markdown_tokens
from ultrakb.storage.artifact_store import ArtifactStore
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.knowledge_store import KnowledgeStore

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="Requires an unprivileged directory symlink")
@pytest.mark.parametrize("zone", ["wiki", "output"])
async def test_mutable_projection_symlink_does_not_control_logical_generator_output(tmp_path, zone):
    root, identity = deck_knowledge(tmp_path)
    scope = storage_scope(root, identity)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / zone).symlink_to(outside, target_is_directory=True)
    template = "wiki/explorations/{slug}.html" if zone == "wiki" else "output/{slug}.html"
    target = template.format(slug="value")
    skill = root / "skills/custom/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: custom\ndescription: Produce an artifact.\nod:\n"
        f"  output_path_template: '{template}'\n---\nWrite the artifact.\n"
    )
    responses = [
        tool_response("write_file", {"path": target, "content": "<html>17</html>"}, "produce"),
        {"content": "Produced."},
    ]
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        preview = preview_generation(root, "deck", "value", skill_name="custom", scope=scope)
        assert preview.target == root / target
        result = await generate_artifact(
            root,
            GenerationOptions("deck", "value", "Present the value.", skill_name="custom"),
            scope=scope,
        )
        assert result.status == "completed" and result.projection_pending, result
        if zone == "wiki":
            assert (
                storage_scope(root, identity).knowledge.read("explorations/value.html")
                == b"<html>17</html>"
            )
        else:
            assert ArtifactStore(root, identity).read(target) == b"<html>17</html>"
        assert not list(outside.iterdir())
        assert len(service.requests) == 2


@pytest.mark.asyncio
async def test_archived_markdown_keeps_its_exact_original_input_revision(tmp_path):
    root, identity = deck_knowledge(tmp_path)
    knowledge, catalog = KnowledgeStore(root, identity), CatalogStore(root, identity)
    original = storage_scope(root, identity).knowledge
    old_unit = original.input_units()[0]
    body = b"# Original claim\n\nThe recorded value was 17.\n"
    knowledge.publish(
        knowledge.prepare_changes(
            original,
            {"explorations/value.md": body},
            page_inputs={"explorations/value.md": (old_unit.unit_revision_id,)},
        )
    )
    old = storage_scope(root, identity).knowledge
    new_file = tmp_path / "updated.md"
    new_file.write_text("The value is now 29.")
    source = catalog.admit(new_file, source_key="fixture")
    processing = catalog.retain_processing(
        source.revision_id,
        {"body.md": new_file},
        body_path="body.md",
        fingerprint="text-v1",
        operation_id="updated-processing",
        decision=classify_markdown_tokens(6),
    )
    new_unit = catalog.plan_unit(processing.processing_id, key="body", doc_name="fixture")
    knowledge.publish(
        knowledge.prepare_revision(
            {
                "index.md": b"# Index",
                "concepts/value.md": b"# New value\n\n29",
                "explorations/value.md": body,
            },
            expected=old.publication_id,
            unit_revisions=(new_unit.unit_revision_id,),
            page_inputs={
                "concepts/value.md": (new_unit.unit_revision_id,),
                "explorations/value.md": (old_unit.unit_revision_id,),
            },
        )
    )
    scope = storage_scope(root, identity)
    before = read_page(root, "explorations/value", scope=scope)
    skill = root / "skills/custom/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: custom\ndescription: Produce an artifact.\nod:\n"
        "  output_path_template: 'wiki/explorations/{slug}.md'\n---\nWrite the artifact.\n"
    )
    responses = [
        tool_response(
            "write_file",
            {"path": "wiki/explorations/value.md", "content": "# Replacement"},
            "produce",
        ),
        {"content": "Produced."},
    ]
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        result = await generate_artifact(
            root,
            GenerationOptions(
                "deck", "value", "Replace.", overwrite="archive", skill_name="custom"
            ),
            scope=scope,
        )
        assert result.status == "completed", result
        current = storage_scope(root, identity)
        path = "explorations/value.md-workspace/iteration-1/value.md"
        archived = read_page(root, path.removesuffix(".md"), scope=current)
        assert current.knowledge.read(path) == body
        assert (
            archived.source_revision_ids
            == before.source_revision_ids
            == (old_unit.source_revision_id,)
        )
        assert tuple(
            unit.unit_revision_id for unit in current.knowledge.input_units(page=path)
        ) == (old_unit.unit_revision_id,)
        assert len(service.requests) == 2
