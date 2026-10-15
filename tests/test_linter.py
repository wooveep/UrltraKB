"""Tests for ultrakb.agent.linter (Task 14)."""

from __future__ import annotations

import pytest

from ultrakb.agent.linter import build_lint_agent, run_knowledge_lint
from ultrakb.schema import SCHEMA_MD


class TestBuildLintAgent:
    def test_agent_name(self, tmp_path):
        agent = build_lint_agent(str(tmp_path), "gpt-4o-mini")
        assert agent.name == "wiki-linter"

    def test_agent_has_two_tools(self, tmp_path):
        agent = build_lint_agent(str(tmp_path), "gpt-4o-mini")
        assert len(agent.tools) == 2

    def test_agent_tool_names(self, tmp_path):
        agent = build_lint_agent(str(tmp_path), "gpt-4o-mini")
        names = {t.name for t in agent.tools}
        assert "list_files" in names
        assert "read_file" in names

    def test_schema_in_instructions(self, tmp_path):
        agent = build_lint_agent(str(tmp_path), "gpt-4o-mini")
        assert SCHEMA_MD in agent.instructions

    def test_agent_model(self, tmp_path):
        agent = build_lint_agent(str(tmp_path), "custom-model")
        assert agent.model == "litellm/custom-model"

    def test_instructions_mention_contradictions(self, tmp_path):
        agent = build_lint_agent(str(tmp_path), "gpt-4o-mini")
        assert "Contradictions" in agent.instructions or "contradictions" in agent.instructions

    def test_instructions_mention_gaps(self, tmp_path):
        agent = build_lint_agent(str(tmp_path), "gpt-4o-mini")
        assert "Gaps" in agent.instructions or "gaps" in agent.instructions


class TestLintAgentParallelToolCalls:
    """Lint never sent parallel_tool_calls before, so its default stays "omit"
    (sending any value, incl. False, breaks Bedrock #175). Explicit config wins.
    """

    def test_unset_omits_the_setting(self, tmp_path):
        from ultrakb.config import set_parallel_tool_calls

        set_parallel_tool_calls(None, False)  # not configured
        agent = build_lint_agent(str(tmp_path), "bedrock/eu.anthropic.claude-sonnet-4-6")
        assert agent.model_settings.parallel_tool_calls is None

    def test_explicit_null_omits(self, tmp_path):
        from ultrakb.config import set_parallel_tool_calls

        set_parallel_tool_calls(None, True)  # explicit null
        agent = build_lint_agent(str(tmp_path), "gpt-4o-mini")
        assert agent.model_settings.parallel_tool_calls is None

    def test_explicit_bools_flow_through(self, tmp_path):
        from ultrakb.config import set_parallel_tool_calls

        set_parallel_tool_calls(True, True)
        assert build_lint_agent(str(tmp_path), "m").model_settings.parallel_tool_calls is True
        set_parallel_tool_calls(False, True)
        assert build_lint_agent(str(tmp_path), "m").model_settings.parallel_tool_calls is False


class TestRunKnowledgeLint:
    @pytest.mark.asyncio
    async def test_returns_final_output(self, kb_dir, live_llm, monkeypatch):
        from ultrakb.agent.query import build_run_config_from_bundle
        from ultrakb.locks import atomic_write_text

        live_llm.install(monkeypatch)
        atomic_write_text(
            kb_dir / "wiki/index.md", "# Index\n[[summaries/orion]]\n[[concepts/orion]]"
        )
        atomic_write_text(
            kb_dir / "wiki/summaries/orion.md",
            "# Orion\nOrion costs 17 credits under standard conditions.",
        )
        atomic_write_text(
            kb_dir / "wiki/concepts/orion.md",
            "# Orion\nOrion costs 23 credits under standard conditions.",
        )
        monkeypatch.setattr("ultrakb.agent.linter.MAX_TURNS", 5)
        result = await run_knowledge_lint(
            kb_dir,
            "deepseek/deepseek-flash",
            bundle=live_llm.credentials,
            run_config=build_run_config_from_bundle(
                "deepseek/deepseek-flash", live_llm.credentials
            ),
        )
        assert "orion" in result.lower() and "17" in result and "23" in result

    def test_calls_runner_with_correct_agent(self, tmp_path):
        agent = build_lint_agent(str(tmp_path), "deepseek/deepseek-flash")
        assert agent.name == "wiki-linter"
        assert {tool.name for tool in agent.tools} == {"list_files", "read_file"}

    def test_handles_empty_final_output(self):
        from ultrakb.agent.linter import semantic_report

        issues = []
        assert "completed" in semantic_report(None, on_issue=issues.append).lower()
        assert issues == ["semantic_report_missing"]
        with pytest.raises(ValueError, match="must be text"):
            semantic_report({"invalid": "report"})
