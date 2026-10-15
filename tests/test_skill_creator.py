"""Tests for ultrakb.skill.creator.

The functional case uses the real budgeted DeepSeek agent. Pure cases test:
  * Tools wire up correctly (write_skill_file is bound to the right path)
  * System prompt gets the intent and skill_name interpolated
  * The skill output dir is created before the agent starts
  * If the agent finishes without writing SKILL.md, we surface an error
  * If the SDK hits its max-turns cap, we translate to RuntimeError so
    the CLI/chat call sites (which only catch RuntimeError) print a
    friendly message instead of leaking a traceback.
"""

from __future__ import annotations

import asyncio

import pytest

from ultrakb.skill.creator import (
    build_skill_create_agent,
    run_skill_create,
)


def _make_kb(tmp_path):
    (tmp_path / "wiki" / "concepts").mkdir(parents=True)
    (tmp_path / "wiki" / "summaries").mkdir(parents=True)
    (tmp_path / "wiki" / "index.md").write_text("# index\n\nNo concepts yet.\n")
    (tmp_path / ".ultrakb").mkdir()
    (tmp_path / ".ultrakb" / "config.yaml").write_text("model: gpt-4o-mini\n")
    from kb_fixtures import mark_current_kb

    mark_current_kb(tmp_path)
    return tmp_path


def _build_demo_agent(kb, model="gpt-4o-mini"):
    return build_skill_create_agent(
        wiki_root=str(kb / "wiki"),
        skill_root=str(kb / "output" / "skills" / "demo"),
        skill_name="demo",
        intent="distill a tax-lookup skill",
        model=model,
    )


def test_build_agent_interpolates_intent_and_name(tmp_path):
    kb = _make_kb(tmp_path)
    agent = _build_demo_agent(kb)
    assert "demo" in agent.instructions
    assert "distill a tax-lookup skill" in agent.instructions


class TestSkillCreateAgentParallelToolCalls:
    """skill-create defaults to True (fan-out read phase; see creator.py), but an
    explicit config value must override it — a Bedrock user's `null` has to reach
    skill creation too, or `ultrakb skill new` stays broken on Bedrock (#175).
    """

    def test_unset_defaults_to_true(self, tmp_path):
        from ultrakb.config import set_parallel_tool_calls

        kb = _make_kb(tmp_path)
        set_parallel_tool_calls(None, False)  # not configured
        agent = _build_demo_agent(kb)
        assert agent.model_settings.parallel_tool_calls is True

    def test_explicit_null_omits_for_bedrock(self, tmp_path):
        from ultrakb.config import set_parallel_tool_calls

        kb = _make_kb(tmp_path)
        set_parallel_tool_calls(None, True)  # explicit null
        agent = _build_demo_agent(kb, model="bedrock/eu.anthropic.claude-sonnet-4-6")
        assert agent.model_settings.parallel_tool_calls is None

    def test_explicit_false_overrides_default(self, tmp_path):
        from ultrakb.config import set_parallel_tool_calls

        kb = _make_kb(tmp_path)
        set_parallel_tool_calls(False, True)  # explicit false
        agent = _build_demo_agent(kb)
        assert agent.model_settings.parallel_tool_calls is False


@pytest.mark.asyncio
async def test_run_skill_create_creates_output_dir(kb_dir, live_llm, monkeypatch):
    from ultrakb.locks import atomic_write_text

    live_llm.install(monkeypatch)
    kb = kb_dir
    from publication_fixtures import publish_document

    atomic_write_text(kb / ".ultrakb/config.yaml", "model: deepseek/deepseek-flash\nlanguage: en\n")
    original = kb / "formatter.txt"
    atomic_write_text(
        original,
        "Bullet-list formatting: prefix each supplied line with a hyphen "
        "and a space; retain its original text and order.",
    )
    imported = await asyncio.to_thread(
        publish_document,
        kb,
        original,
        original.read_text(),
        bundle=live_llm.credentials,
    )
    assert imported.status == "added", imported.message
    target = kb / "output/skills/demo"
    result = await run_skill_create(
        kb_dir=kb,
        skill_name="demo",
        intent="Distill a bullet-list formatter skill. The sole source is formatter: "
        "verify its 112-character original with get_text_content, then write SKILL.md "
        "once, with name and description frontmatter and at most 120 words total. "
        "No examples or supporting files. Call done and finish after writing.",
        model="deepseek/deepseek-flash",
        bundle=live_llm.credentials,
        max_turns=6,
    )
    assert result == target and (target / "SKILL.md").exists()
    from ultrakb import frontmatter

    metadata = frontmatter.parse((target / "SKILL.md").read_text())
    assert metadata["name"] == "demo" and metadata["description"]


@pytest.mark.asyncio
async def test_run_skill_create_raises_when_no_skill_md_written(tmp_path):
    from ultrakb.skill.creator import finish_skill_create

    kb = _make_kb(tmp_path)
    target = kb / "output/skills/demo"
    target.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="did not write SKILL.md"):
        finish_skill_create(target)


@pytest.mark.asyncio
async def test_run_skill_create_translates_max_turns_to_runtime_error(tmp_path):
    kb = _make_kb(tmp_path)
    with pytest.raises(RuntimeError, match="step cap"):
        await run_skill_create(
            kb_dir=kb, skill_name="demo", intent="x", model="deepseek/deepseek-flash", max_turns=0
        )
