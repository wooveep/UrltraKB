"""Tests for ultrakb list and ultrakb status CLI commands."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner
from publication_read_inputs import publish_fixture

from ultrakb.cli import cli
from ultrakb.desktop.verification_data import retain_document


def _setup_kb(tmp_path: Path) -> Path:
    """Create a minimal KB structure and return kb_dir."""
    kb_dir = tmp_path
    (kb_dir / "raw").mkdir()
    (kb_dir / "wiki" / "sources" / "images").mkdir(parents=True)
    (kb_dir / "wiki" / "summaries").mkdir(parents=True)
    (kb_dir / "wiki" / "concepts").mkdir(parents=True)
    (kb_dir / "wiki" / "entities").mkdir(parents=True)
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


def _retain_documents(kb, records):
    for item in records.values():
        name = item["name"]
        pages = (
            [{"page": 1, "unit_kind": "page", "content": "A retained PDF source."}]
            if name.endswith(".pdf")
            else None
        )
        retain_document(kb, name, Path(name).stem, "A retained source.", pages=pages)


class TestListCommand:
    def test_list_no_kb(self, tmp_path):
        runner = CliRunner()
        with (
            runner.isolated_filesystem(temp_dir=tmp_path),
            patch("ultrakb.cli._find_kb_dir", return_value=None),
        ):
            result = runner.invoke(cli, ["list"])
            assert "No knowledge base found" in result.output

    def test_list_empty_kb(self, tmp_path):
        kb_dir = _setup_kb(tmp_path)
        publish_fixture(kb_dir)
        runner = CliRunner()
        with patch("ultrakb.cli._find_kb_dir", return_value=kb_dir):
            result = runner.invoke(cli, ["list"])
            assert "No documents indexed yet" in result.output

    def test_list_shows_documents(self, tmp_path):
        kb_dir = _setup_kb(tmp_path)
        hashes = {
            "abc123": {"name": "paper.pdf", "type": "pdf"},
            "def456": {"name": "notes.md", "type": "md"},
        }
        _retain_documents(kb_dir, hashes)

        publish_fixture(kb_dir)
        runner = CliRunner()
        with patch("ultrakb.cli._find_kb_dir", return_value=kb_dir):
            result = runner.invoke(cli, ["list"])

        assert "paper.pdf" in result.output
        assert "notes.md" in result.output
        assert "pdf" in result.output
        assert "md" in result.output

    def test_list_shows_concepts(self, tmp_path):
        kb_dir = _setup_kb(tmp_path)
        hashes = {"abc": {"name": "paper.pdf", "type": "pdf"}}
        _retain_documents(kb_dir, hashes)
        (kb_dir / "wiki" / "concepts" / "attention.md").write_text("# Attention")
        (kb_dir / "wiki" / "concepts" / "transformer.md").write_text("# Transformer")

        publish_fixture(kb_dir)
        runner = CliRunner()
        with patch("ultrakb.cli._find_kb_dir", return_value=kb_dir):
            result = runner.invoke(cli, ["list"])

        assert "attention" in result.output
        assert "transformer" in result.output

    def test_list_no_concepts_section_when_empty(self, tmp_path):
        kb_dir = _setup_kb(tmp_path)
        hashes = {"abc": {"name": "paper.pdf", "type": "pdf"}}
        _retain_documents(kb_dir, hashes)

        publish_fixture(kb_dir)
        runner = CliRunner()
        with patch("ultrakb.cli._find_kb_dir", return_value=kb_dir):
            result = runner.invoke(cli, ["list"])

        assert result.exit_code == 0
        # No concepts in output since none exist
        assert "Concepts:" not in result.output

    def test_list_shows_entities(self, tmp_path):
        kb_dir = _setup_kb(tmp_path)
        hashes = {"abc": {"name": "paper.pdf", "type": "pdf"}}
        _retain_documents(kb_dir, hashes)
        (kb_dir / "wiki" / "entities" / "ada-lovelace.md").write_text("# Ada")
        (kb_dir / "wiki" / "entities" / "openai.md").write_text("# OpenAI")

        publish_fixture(kb_dir)
        runner = CliRunner()
        with patch("ultrakb.cli._find_kb_dir", return_value=kb_dir):
            result = runner.invoke(cli, ["list"])

        assert "Entities (2):" in result.output
        assert "ada-lovelace" in result.output
        assert "openai" in result.output

    def test_list_no_entities_section_when_empty(self, tmp_path):
        kb_dir = _setup_kb(tmp_path)
        hashes = {"abc": {"name": "paper.pdf", "type": "pdf"}}
        _retain_documents(kb_dir, hashes)

        publish_fixture(kb_dir)
        runner = CliRunner()
        with patch("ultrakb.cli._find_kb_dir", return_value=kb_dir):
            result = runner.invoke(cli, ["list"])

        assert result.exit_code == 0
        assert "Entities:" not in result.output
        assert "Entities (" not in result.output


class TestStatusCommand:
    def test_status_no_kb(self, tmp_path):
        runner = CliRunner()
        with (
            runner.isolated_filesystem(temp_dir=tmp_path),
            patch("ultrakb.cli._find_kb_dir", return_value=None),
        ):
            result = runner.invoke(cli, ["status"])
            assert "No knowledge base found" in result.output

    def test_status_shows_directory_counts(self, tmp_path):
        kb_dir = _setup_kb(tmp_path)
        # Add some files
        (kb_dir / "wiki" / "sources" / "doc1.md").write_text("# Doc 1")
        (kb_dir / "wiki" / "sources" / "doc2.md").write_text("# Doc 2")
        (kb_dir / "wiki" / "summaries" / "sum1.md").write_text("# Sum 1")
        (kb_dir / "wiki" / "concepts" / "concept1.md").write_text("# Concept")

        publish_fixture(kb_dir)
        runner = CliRunner()
        with patch("ultrakb.cli._find_kb_dir", return_value=kb_dir):
            result = runner.invoke(cli, ["status"])

        assert "sources" in result.output
        assert "summaries" in result.output
        assert "concepts" in result.output
        assert "entities" in result.output
        assert "reports" in result.output

    def test_status_shows_total_indexed(self, tmp_path):
        kb_dir = _setup_kb(tmp_path)
        hashes = {
            "abc": {"name": "a.pdf", "type": "pdf"},
            "def": {"name": "b.pdf", "type": "pdf"},
            "ghi": {"name": "c.md", "type": "md"},
        }
        _retain_documents(kb_dir, hashes)

        publish_fixture(kb_dir)
        runner = CliRunner()
        with patch("ultrakb.cli._find_kb_dir", return_value=kb_dir):
            result = runner.invoke(cli, ["status"])

        assert "3" in result.output  # total indexed count

    def test_status_shows_raw_count(self, tmp_path):
        kb_dir = _setup_kb(tmp_path)
        (kb_dir / "raw" / "file1.pdf").write_bytes(b"PDF")
        (kb_dir / "raw" / "file2.pdf").write_bytes(b"PDF")

        publish_fixture(kb_dir)
        runner = CliRunner()
        with patch("ultrakb.cli._find_kb_dir", return_value=kb_dir):
            result = runner.invoke(cli, ["status"])

        assert "raw" in result.output

    def test_status_exit_code_zero(self, tmp_path):
        kb_dir = _setup_kb(tmp_path)

        publish_fixture(kb_dir)
        runner = CliRunner()
        with patch("ultrakb.cli._find_kb_dir", return_value=kb_dir):
            result = runner.invoke(cli, ["status"])

        assert result.exit_code == 0


class TestStatusKbPath:
    """Status output must lead with the active KB path so agents and
    scripts can locate the wiki when invoked from outside the KB root."""

    def test_status_prints_kb_path_first(self, tmp_path):
        kb_dir = _setup_kb(tmp_path)

        publish_fixture(kb_dir)
        runner = CliRunner()
        with patch("ultrakb.cli._find_kb_dir", return_value=kb_dir):
            result = runner.invoke(cli, ["status"])

        assert result.exit_code == 0
        # First non-empty line carries the path in a parseable form:
        #   "Knowledge base: /path/to/kb"
        first_line = result.output.splitlines()[0]
        assert first_line.startswith("Knowledge base: ")
        assert first_line.split(": ", 1)[1] == str(kb_dir)
