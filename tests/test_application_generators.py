"""Generator consent, independent archives, and retained partial artifacts."""

import asyncio

from agents.tool_context import ToolContext

pytest_plugins = ("test_vendor_sdk",)


async def invoke_write(tool, arguments):
    context = ToolContext(None, tool_name=tool.name, tool_call_id="test", tool_arguments=arguments)
    return await tool.on_invoke_tool(context, arguments)


def test_real_short_deck_generation_keeps_requested_fact(kb_dir, model_service):
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.generators import GenerationOptions, generate_artifact
    from ultrakb.locks import atomic_write_text

    compiled_kb(kb_dir)
    custom_deck(kb_dir)
    atomic_write_text(
        kb_dir / ".ultrakb/config.yaml",
        "model: deepseek/deepseek-flash\nlanguage: en\nmodel_capacity:\n"
        "  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\nmodel_policy:\n"
        "  max_calls: 4\n  retries: 0\n  deadline_seconds: 30\nrole_generation:\n"
        "  answer:\n    max_tokens: 256\n    reasoning_effort: none\n",
    )
    # Five short slides meet the generic grammar's hard minimum while the
    # entire write_file JSON fits comfortably within the actual 256-token cap.
    html = (
        "<!doctype html><html><body>"
        + "".join(
            f'<section class="slide">{text}</section>'
            for text in ("Orion 17 credits", "Plan", "Track", "Approve", "Done")
        )
        + "</body></html>"
    )
    context = ExecutionContext()
    result = asyncio.run(
        generate_artifact(
            kb_dir,
            GenerationOptions(
                "deck",
                "budget",
                "Write only output/shared/index.html using one write_file call. "
                "Use this user-supplied HTML, then finish: " + html,
                skill_name="custom",
            ),
            context=context,
        )
    )
    assert result.status == "completed", result
    assert result.validation.ok, result.validation
    assert "17 credits" in (kb_dir / "output/shared/index.html").read_text()
    assert context.executor.policy.max_calls == 4
    assert context.executor.policy.retries == 0
    assert context.executor.calls <= 4
    assert context.executor.bindings.generation_options("answer")["max_tokens"] == 256
    assert all(request["max_tokens"] == 256 for request in model_service.requests)
    assert 2 <= len(model_service.requests) <= 4


def compiled_kb(kb_dir):
    (kb_dir / "wiki/concepts/topic.md").write_text("# Topic\nUseful knowledge")
    from publication_read_inputs import publish_fixture

    publish_fixture(kb_dir)
    return kb_dir


def custom_deck(kb_dir, template="output/shared/index.html"):
    skill = kb_dir / "skills/custom/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: custom\ndescription: Generate test decks\n"
        f"od:\n  mode: deck\n  output_path_template: {template}\n---\nCreate a deck."
    )


def test_custom_deck_existing_actual_target_requires_consent(kb_dir):
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.generators import (
        GenerationOptions,
        generate_artifact,
        preview_generation,
    )

    compiled_kb(kb_dir)
    custom_deck(kb_dir)
    actual = kb_dir / "output/shared/index.html"
    actual.parent.mkdir(parents=True)
    actual.write_text("Original custom output")
    from artifact_fixtures import publish_artifacts

    publish_artifacts(kb_dir, actual.parent)
    preview = preview_generation(kb_dir, "deck", "different-slug", skill_name="custom")
    assert preview.target == actual.parent and preview.exists
    context = ExecutionContext()
    result = asyncio.run(
        generate_artifact(
            kb_dir,
            GenerationOptions(
                "deck",
                "different-slug",
                "Deck",
                version=preview.version,
                skill_name="custom",
            ),
            context=context,
        )
    )
    assert result.status == "conflict"
    assert context.executor is None or context.executor.calls == 0
    assert actual.read_text() == "Original custom output"


def test_missing_deck_skill_is_rejected_before_snapshot_or_archive(kb_dir):
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.generators import GenerationOptions, generate_artifact

    compiled_kb(kb_dir)
    target = kb_dir / "output/decks/demo"
    target.mkdir(parents=True)
    (target / "index.html").write_text("Original")
    context = ExecutionContext()
    result = asyncio.run(
        generate_artifact(
            kb_dir,
            GenerationOptions(
                "deck",
                "demo",
                "Deck",
                overwrite="archive",
                skill_name="nonexistent-verification-skill",
            ),
            context=context,
        )
    )
    assert result.status == "invalid"
    assert context.snapshot is None
    assert (target / "index.html").read_text() == "Original"
    assert not (target.parent / "demo-workspace").exists()


def test_generation_conflicts_on_a_changed_supporting_file_before_snapshot(kb_dir):
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.generators import (
        GenerationOptions,
        generate_artifact,
        preview_generation,
    )

    compiled_kb(kb_dir)
    target = kb_dir / "output/skills/demo"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("Old skill")
    ref = target / "references/source.md"
    ref.parent.mkdir()
    ref.write_text("Original supporting material")
    from artifact_fixtures import publish_artifacts

    publish_artifacts(kb_dir, target)
    preview = preview_generation(kb_dir, "skill", "demo")
    ref.write_text("Edited after confirmation")
    publish_artifacts(kb_dir, target)
    context = ExecutionContext()
    result = asyncio.run(
        generate_artifact(
            kb_dir,
            GenerationOptions(
                "skill", "demo", "Explain topic", overwrite="archive", version=preview.version
            ),
            context=context,
        )
    )
    assert result.status == "conflict"
    assert context.executor is None or context.executor.calls == 0
    assert ref.read_text() == "Edited after confirmation"
    assert not (target.parent / "demo-workspace").exists()
