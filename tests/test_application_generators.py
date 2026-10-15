"""Generator consent, independent archives, and retained partial artifacts."""

import asyncio
import json
from pathlib import Path

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


def test_deck_tools_cannot_overwrite_archives_or_unconfirmed_other_artifacts(kb_dir, monkeypatch):
    from ultrakb.application.generators import GenerationOptions, generate_artifact

    compiled_kb(kb_dir)
    custom_deck(kb_dir)
    actual = kb_dir / "output/shared/index.html"
    actual.parent.mkdir(parents=True)
    actual.write_text("Original custom output")
    other = kb_dir / "output/unrelated.html"
    other.write_text("Other artifact")

    async def supplied_deck(*, kb_dir, deck_name, intent, model, prepared, **kwargs):
        from ultrakb.agent.skill_runner import finish_skill_run, prepare_skill_run

        # Explicit artifact inputs verify writer permissions and publication.
        request = prepare_skill_run(
            skill_name="custom",
            intent=intent,
            kb_dir=kb_dir,
            model=model,
            slug=deck_name,
            prepared=prepared,
        )
        tool = next(tool for tool in request.agent.tools if tool.name == "write_file")
        for path in ("output/shared-workspace/iteration-1/index.html", "output/unrelated.html"):
            result = await invoke_write(tool, json.dumps({"path": path, "content": "Clobbered"}))
            assert "Access denied" in result
        await invoke_write(
            tool, '{"path":"output/shared/index.html","content":"<html>New deck</html>"}'
        )
        return finish_skill_run(request.definition)

    monkeypatch.setattr("ultrakb.skill.generator.run_deck_create", supplied_deck)
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
    from ultrakb.application.generators import GenerationOptions, generate_artifact

    compiled_kb(kb_dir)
    target = kb_dir / "output/skills/demo"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("Old skill")

    async def interrupted_delivery(*, kb_dir, skill_name, intent, model, **kwargs):
        from ultrakb.skill import skill_dir
        from ultrakb.skill.creator import build_skill_create_agent

        target = skill_dir(kb_dir, skill_name)
        agent = build_skill_create_agent(
            wiki_root=str(kb_dir / "wiki"),
            skill_root=str(target),
            intent=intent,
            skill_name=skill_name,
            model=model,
        )
        tool = next(tool for tool in agent.tools if tool.name == "write_skill_file")
        await invoke_write(tool, '{"path":"references/partial.md","content":"Retained source"}')
        raise ConnectionError("Private delivery detail")

    monkeypatch.setattr("ultrakb.skill.generator.run_skill_create", interrupted_delivery)
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
    assert not list((kb_dir / ".ultrakb/journal").glob("*.json"))


def test_generated_invalid_skill_remains_an_artifact_with_quality_issues(kb_dir, monkeypatch):
    from ultrakb.application.generators import GenerationOptions, generate_artifact

    compiled_kb(kb_dir)

    async def supplied_invalid(*, kb_dir, skill_name, intent, model, **kwargs):
        from ultrakb.skill import skill_dir
        from ultrakb.skill.creator import build_skill_create_agent, finish_skill_create

        # Invalid metadata is domain input, independent of model behavior.
        target = skill_dir(kb_dir, skill_name)
        agent = build_skill_create_agent(
            wiki_root=str(kb_dir / "wiki"),
            skill_root=str(target),
            intent=intent,
            skill_name=skill_name,
            model=model,
        )
        tool = next(tool for tool in agent.tools if tool.name == "write_skill_file")
        await invoke_write(tool, '{"path":"SKILL.md","content":"# Missing metadata"}')
        return finish_skill_create(target)

    monkeypatch.setattr("ultrakb.skill.generator.run_skill_create", supplied_invalid)
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
    from ultrakb.application.generators import GenerationOptions, generate_artifact

    compiled_kb(kb_dir)
    (kb_dir / ".claude-plugin/marketplace.json").mkdir(parents=True)

    async def supplied_artifact(*, kb_dir, skill_name, intent, model, **kwargs):
        from ultrakb.skill import skill_dir
        from ultrakb.skill.creator import build_skill_create_agent, finish_skill_create

        target = skill_dir(kb_dir, skill_name)
        agent = build_skill_create_agent(
            wiki_root=str(kb_dir / "wiki"),
            skill_root=str(target),
            intent=intent,
            skill_name=skill_name,
            model=model,
        )
        tool = next(tool for tool in agent.tools if tool.name == "write_skill_file")
        await invoke_write(tool, '{"path":"SKILL.md","content":"# Generated artifact"}')
        return finish_skill_create(target)

    monkeypatch.setattr("ultrakb.skill.generator.run_skill_create", supplied_artifact)
    result = asyncio.run(
        generate_artifact(kb_dir, GenerationOptions("skill", "demo", "Explain topic"))
    )
    assert result.status == "failed"
    assert result.unfinished == ("marketplace",)
    assert str(kb_dir / "output/skills/demo/SKILL.md") in result.resources
    assert (kb_dir / "output/skills/demo/SKILL.md").read_text() == "# Generated artifact"
