"""Tests for the ultrakb lint CLI command."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from ultrakb.cli import cli


def _setup_kb(tmp_path: Path) -> Path:
    """Create a minimal KB structure and return kb_dir."""
    kb_dir = tmp_path
    (kb_dir / "raw").mkdir()
    (kb_dir / "wiki" / "sources" / "images").mkdir(parents=True)
    (kb_dir / "wiki" / "summaries").mkdir(parents=True)
    (kb_dir / "wiki" / "concepts").mkdir(parents=True)
    (kb_dir / "wiki" / "reports").mkdir(parents=True)
    ultrakb_dir = kb_dir / ".ultrakb"
    ultrakb_dir.mkdir()
    (ultrakb_dir / "config.yaml").write_text("model: gpt-4o-mini\n")
    from kb_fixtures import mark_current_kb

    mark_current_kb(ultrakb_dir.parent)
    (kb_dir / "wiki" / "index.md").write_text(
        "# Knowledge Base Index\n\n## Documents\n\n## Concepts\n"
    )
    return kb_dir


class TestLintCommand:
    def test_lint_empty_kb_records_a_structural_report_without_model_sends(self, tmp_path):
        """Lint on an empty KB (no indexed docs) should exit early."""
        kb_dir = _setup_kb(tmp_path)
        runner = CliRunner()
        with patch("ultrakb.cli._find_kb_dir", return_value=kb_dir):
            result = runner.invoke(cli, ["lint"])
        assert result.exit_code == 0
        assert "Structural Lint Report" in result.output
        from native_usage_fixtures import read_requests

        assert read_requests(kb_dir) == []
        assert len(list((kb_dir / "wiki/reports").glob("*.md"))) == 1

    def test_lint_ignores_an_unrelated_legacy_registry(self, tmp_path):
        """Lint should also skip when hashes.json doesn't exist."""
        kb_dir = _setup_kb(tmp_path)
        (kb_dir / ".ultrakb/hashes.json").write_text("not native business state")
        runner = CliRunner()
        with patch("ultrakb.cli._find_kb_dir", return_value=kb_dir):
            result = runner.invoke(cli, ["lint"])
        assert result.exit_code == 0
        assert "Structural Lint Report" in result.output

    def test_lint_no_kb(self, tmp_path):
        runner = CliRunner()
        with (
            runner.isolated_filesystem(temp_dir=tmp_path),
            patch("ultrakb.cli._find_kb_dir", return_value=None),
        ):
            result = runner.invoke(cli, ["lint"])
            assert "No knowledge base found" in result.output

    def test_lint_runs_when_docs_exist(self, tmp_path, live_llm, monkeypatch):
        """Lint should proceed when there are indexed documents."""
        from application_llm_fixtures import (
            assert_semantic_lint_delivery,
            semantic_lint_fixture,
        )

        kb_dir = _setup_kb(tmp_path)
        observation = semantic_lint_fixture(kb_dir, live_llm, monkeypatch)
        runner = CliRunner()
        with patch("ultrakb.cli._find_kb_dir", return_value=kb_dir):
            result = runner.invoke(cli, ["lint"])
        assert result.exit_code == 0
        assert "Running structural lint" in result.output
        assert "Running knowledge lint" in result.output
        assert "Report written to" in result.output
        assert len(observation.reports) == 1
        assert_semantic_lint_delivery(observation.reports[0], observation, live_llm)
        saved = list((kb_dir / "wiki/reports").glob("lint_*.md"))
        assert len(saved) == 1
        assert observation.reports[0] in saved[0].read_text(encoding="utf-8")
