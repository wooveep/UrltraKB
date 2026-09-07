"""Generator consent, independent archives, and retained partial artifacts."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from agents import Runner
from agents.tool_context import ToolContext


async def invoke_write(tool, arguments):
    context = ToolContext(None, tool_name=tool.name, tool_call_id="test", tool_arguments=arguments)
    return await tool.on_invoke_tool(context, arguments)


def compiled_kb(kb_dir):
    (kb_dir / "wiki/concepts/topic.md").write_text("# Topic\nUseful knowledge")
    return kb_dir


def custom_deck(kb_dir, template="output/shared/index.html"):
    skill = kb_dir / "skills/custom/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: custom\ndescription: Generate test decks\n"
        f"od:\n  mode: deck\n  output_path_template: {template}\n---\nCreate a deck."
    )


def test_custom_deck_existing_actual_target_requires_consent(kb_dir):
    from openkb.application.execution import ExecutionContext
    from openkb.application.generators import (
        GenerationOptions,
        generate_artifact,
        preview_generation,
    )

    compiled_kb(kb_dir)
    custom_deck(kb_dir)
    actual = kb_dir / "output/shared/index.html"
    actual.parent.mkdir(parents=True)
    actual.write_text("Original custom output")
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
    assert context.snapshot is None
    assert actual.read_text() == "Original custom output"


def test_missing_deck_skill_is_rejected_before_snapshot_or_archive(kb_dir):
    from openkb.application.execution import ExecutionContext
    from openkb.application.generators import GenerationOptions, generate_artifact

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


def test_deck_tools_cannot_overwrite_archives_or_unconfirmed_other_artifacts(kb_dir, monkeypatch):
    from openkb.application.generators import GenerationOptions, generate_artifact

    compiled_kb(kb_dir)
    custom_deck(kb_dir)
    actual = kb_dir / "output/shared/index.html"
    actual.parent.mkdir(parents=True)
    actual.write_text("Original custom output")
    other = kb_dir / "output/unrelated.html"
    other.write_text("Other artifact")

    async def writes(agent, *args, **kwargs):
        tool = next(tool for tool in agent.tools if tool.name == "write_file")
        for path in ("output/shared-workspace/iteration-1/index.html", "output/unrelated.html"):
            result = await invoke_write(tool, json.dumps({"path": path, "content": "Clobbered"}))
            assert "Access denied" in result
        await invoke_write(
            tool, '{"path":"output/shared/index.html","content":"<html>New deck</html>"}'
        )
        return SimpleNamespace(final_output="Done")

    monkeypatch.setattr(Runner, "run", writes)
    result = asyncio.run(
        generate_artifact(
            kb_dir,
            GenerationOptions(
                "deck",
                "different-slug",
                "Deck",
                overwrite="archive",
                skill_name="custom",
            ),
        )
    )
    assert result.status == "completed", result
    assert result.output_dir == actual.parent
    assert (result.archive_path / "index.html").read_text() == "Original custom output"
    assert "history_write_denied" in result.quality
    assert "unconfirmed_output_write_denied" in result.quality
    assert other.read_text() == "Other artifact"


def test_generation_conflicts_on_a_changed_supporting_file_before_snapshot(kb_dir):
    from openkb.application.execution import ExecutionContext
    from openkb.application.generators import (
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
    preview = preview_generation(kb_dir, "skill", "demo")
    ref.write_text("Edited after confirmation")
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
    assert context.snapshot is None
    assert ref.read_text() == "Edited after confirmation"
    assert not (target.parent / "demo-workspace").exists()


def test_generation_keeps_archived_version_and_partial_files_on_model_failure(kb_dir, monkeypatch):
    from openkb.application.generators import GenerationOptions, generate_artifact

    compiled_kb(kb_dir)
    target = kb_dir / "output/skills/demo"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("Old skill")

    async def partial(agent, *args, **kwargs):
        # Exercise the real agent write tool at the SDK boundary.
        tool = next(tool for tool in agent.tools if tool.name == "write_skill_file")
        await invoke_write(tool, '{"path":"references/partial.md","content":"Retained source"}')
        raise ConnectionError("Private provider detail")

    monkeypatch.setattr(Runner, "run", partial)
    result = asyncio.run(
        generate_artifact(
            kb_dir, GenerationOptions("skill", "demo", "Explain topic", overwrite="archive")
        )
    )
    assert result.status == "failed", result
    assert result.error_type == "ConnectionError", result
    assert Path(result.archive_path, "SKILL.md").read_text() == "Old skill"
    assert (target / "references/partial.md").read_text() == "Retained source"
    assert str(target / "references/partial.md") in result.resources
    assert "generation" in result.unfinished
    assert not list((kb_dir / ".openkb/journal").glob("*.json"))


def test_generated_invalid_skill_remains_an_artifact_with_quality_issues(kb_dir, monkeypatch):
    from openkb.application.generators import GenerationOptions, generate_artifact

    compiled_kb(kb_dir)

    async def invalid(agent, *args, **kwargs):
        tool = next(tool for tool in agent.tools if tool.name == "write_skill_file")
        await invoke_write(tool, '{"path":"SKILL.md","content":"# Missing metadata"}')
        return SimpleNamespace(final_output="Done")

    monkeypatch.setattr(Runner, "run", invalid)
    result = asyncio.run(
        generate_artifact(kb_dir, GenerationOptions("skill", "demo", "Explain topic"))
    )
    assert result.status == "completed", result
    assert result.validation.errors
    assert "validation_errors" in result.quality
    assert str(kb_dir / "output/skills/demo/SKILL.md") in result.resources
    assert (kb_dir / ".claude-plugin/marketplace.json").is_file()


def test_manifest_failure_preserves_generated_files_and_names_the_unfinished_stage(
    kb_dir, monkeypatch
):
    from openkb.application.generators import GenerationOptions, generate_artifact

    compiled_kb(kb_dir)
    (kb_dir / ".claude-plugin/marketplace.json").mkdir(parents=True)

    async def produce(agent, *args, **kwargs):
        tool = next(tool for tool in agent.tools if tool.name == "write_skill_file")
        await invoke_write(tool, '{"path":"SKILL.md","content":"# Generated artifact"}')
        return SimpleNamespace(final_output="Done")

    monkeypatch.setattr(Runner, "run", produce)
    result = asyncio.run(
        generate_artifact(kb_dir, GenerationOptions("skill", "demo", "Explain topic"))
    )
    assert result.status == "failed"
    assert result.unfinished == ("marketplace",)
    assert str(kb_dir / "output/skills/demo/SKILL.md") in result.resources
    assert (kb_dir / "output/skills/demo/SKILL.md").read_text() == "# Generated artifact"
