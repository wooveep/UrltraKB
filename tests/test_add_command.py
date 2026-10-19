"""Tests for the `add` CLI command (Task 10)."""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from ultrakb.cli import SUPPORTED_EXTENSIONS, _find_kb_dir, cli

pytest_plugins = ("test_pdf_readback",)


class TestSupportedExtensions:
    def test_pdf_supported(self):
        assert ".pdf" in SUPPORTED_EXTENSIONS

    def test_md_supported(self):
        assert ".md" in SUPPORTED_EXTENSIONS

    def test_docx_supported(self):
        assert ".docx" in SUPPORTED_EXTENSIONS

    def test_txt_supported(self):
        assert ".txt" in SUPPORTED_EXTENSIONS

    def test_unknown_not_supported(self):
        assert ".xyz" not in SUPPORTED_EXTENSIONS


class TestFindKbDir:
    def test_finds_ultrakb_dir(self, tmp_path, monkeypatch):
        (tmp_path / ".ultrakb").mkdir()
        (tmp_path / ".ultrakb/config.yaml").write_text("{}")
        from kb_fixtures import mark_current_kb

        mark_current_kb(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = _find_kb_dir()
        assert result is not None

    def test_returns_none_if_no_ultrakb(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with patch("ultrakb.cli.load_global_config", return_value={}):
            result = _find_kb_dir()
            assert result is None

    def test_lifecycle_metadata_is_not_an_initialized_knowledge_base(self, tmp_path, monkeypatch):
        (tmp_path / ".ultrakb/kb-lifecycle").mkdir(parents=True)
        nested = tmp_path / "project"
        nested.mkdir()
        monkeypatch.chdir(nested)
        with patch("ultrakb.cli.load_global_config", return_value={}):
            result = CliRunner().invoke(cli, ["status"])
        assert "No knowledge base found" in result.output

    def test_damaged_current_kb_does_not_fall_back_to_another_default(self, tmp_path, monkeypatch):
        from ultrakb.storage.database import StorageFormatError

        damaged, other = tmp_path / "damaged", tmp_path / "other"
        for root in (damaged, other):
            (root / ".ultrakb").mkdir(parents=True)
            (root / "wiki").mkdir()
        (damaged / ".ultrakb/needs-repair.json").write_text("{}")
        (other / ".ultrakb/config.yaml").write_text("{}")
        from kb_fixtures import mark_current_kb

        mark_current_kb(other)
        monkeypatch.chdir(damaged)
        with patch("ultrakb.cli.load_global_config", return_value={"default_kb": str(other)}):
            result = CliRunner().invoke(cli, ["status"])
        assert isinstance(result.exception, StorageFormatError)
        assert not (damaged / ".ultrakb/kb.sqlite").exists()


class TestAddCommand:
    def _setup_kb(self, tmp_path):
        """Create a minimal KB structure."""
        (tmp_path / "raw").mkdir()
        (tmp_path / "wiki" / "sources" / "images").mkdir(parents=True)
        (tmp_path / "wiki" / "summaries").mkdir(parents=True)
        (tmp_path / "wiki" / "concepts").mkdir(parents=True)
        (tmp_path / "wiki" / "reports").mkdir(parents=True)
        ultrakb_dir = tmp_path / ".ultrakb"
        ultrakb_dir.mkdir()
        (ultrakb_dir / "config.yaml").write_text("model: gpt-4o-mini\n")
        from kb_fixtures import mark_current_kb

        mark_current_kb(ultrakb_dir.parent)
        return tmp_path

    def test_add_missing_init(self, tmp_path):
        runner = CliRunner()
        with (
            runner.isolated_filesystem(temp_dir=tmp_path),
            patch("ultrakb.cli._find_kb_dir", return_value=None),
        ):
            result = runner.invoke(cli, ["add", "somefile.pdf"])
            assert "No knowledge base found" in result.output

    def test_add_single_file_calls_helper(self, tmp_path):
        kb_dir = self._setup_kb(tmp_path)
        doc = tmp_path / "test.md"
        doc.write_text("# Hello")

        runner = CliRunner()
        with (
            patch("ultrakb.cli.add_single_file") as mock_add,
            patch("ultrakb.cli._find_kb_dir", return_value=kb_dir),
        ):
            runner.invoke(cli, ["add", str(doc)])
            assert mock_add.call_count == 1
            assert mock_add.call_args.args == (doc, kb_dir)
            assert mock_add.call_args.kwargs.get("scope") is None
            assert mock_add.call_args.kwargs.get("metadata") is None

    def test_add_forwards_concurrency_from_config(self, tmp_path):
        from unittest.mock import AsyncMock

        from ultrakb.cli import add_single_file

        kb_dir = self._setup_kb(tmp_path)
        (kb_dir / ".ultrakb" / "config.yaml").write_text(
            "model: gpt-4o-mini\ncompile_concurrency: 3\n", encoding="utf-8"
        )
        from kb_fixtures import mark_current_kb

        mark_current_kb(kb_dir)
        doc = tmp_path / "notes.md"
        doc.write_text("# Notes\n\nBody", encoding="utf-8")

        with (
            patch(
                "ultrakb.agent.compiler.compile_short_doc", new_callable=AsyncMock
            ) as mock_compile,
            patch("ultrakb.cli._setup_llm_key"),
        ):
            outcome = add_single_file(doc, kb_dir)

        assert outcome == "added"
        assert mock_compile.call_args.kwargs["max_concurrency"] == 3

    def test_add_single_file_publishes_an_immutable_source(self, tmp_path, pdf_model):
        from ultrakb.application.knowledge_bases import get_kb_list
        from ultrakb.cli import add_single_file
        from ultrakb.documents import read_document_source

        kb = self._setup_kb(tmp_path)
        doc = tmp_path / "coordinated.md"
        doc.write_text("# Coordinated\n", encoding="utf-8")
        assert add_single_file(doc, kb) == "added"
        saved = read_document_source(kb, get_kb_list(kb)["documents"][0]["hash"])
        assert saved["content"] == "# Coordinated\n"
        assert saved["knowledge_revision_id"] is not None

    def test_add_directory_calls_helper_for_each_file(self, tmp_path):
        kb_dir = self._setup_kb(tmp_path)
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        (docs_dir / "a.md").write_text("# A")
        (docs_dir / "b.txt").write_text("B content")
        (docs_dir / "ignore.xyz").write_text("skip me")

        runner = CliRunner()
        with (
            patch("ultrakb.cli.add_single_file") as mock_add,
            patch("ultrakb.cli._find_kb_dir", return_value=kb_dir),
        ):
            runner.invoke(cli, ["add", str(docs_dir)])
            # Should be called for .md and .txt but not .xyz
            assert mock_add.call_count == 2
            called_names = {call.args[0].name for call in mock_add.call_args_list}
            assert "a.md" in called_names
            assert "b.txt" in called_names
            assert "ignore.xyz" not in called_names

    def test_add_unsupported_extension(self, tmp_path):
        kb_dir = self._setup_kb(tmp_path)
        doc = tmp_path / "file.xyz"
        doc.write_text("content")

        runner = CliRunner()
        with patch("ultrakb.cli._find_kb_dir", return_value=kb_dir):
            result = runner.invoke(cli, ["add", str(doc)])
            assert "Unsupported file type" in result.output

    def test_add_nonexistent_path(self, tmp_path):
        kb_dir = self._setup_kb(tmp_path)

        runner = CliRunner()
        with patch("ultrakb.cli._find_kb_dir", return_value=kb_dir):
            result = runner.invoke(cli, ["add", str(tmp_path / "nonexistent.pdf")])
            assert "does not exist" in result.output

    def test_add_skipped_file(self, tmp_path, pdf_model):
        kb = self._setup_kb(tmp_path)
        doc = tmp_path / "test.md"
        doc.write_text("# Hello")
        runner = CliRunner()
        arguments = ["--kb-dir", str(kb), "add", str(doc)]
        first = runner.invoke(cli, arguments)
        assert first.exit_code == 0 and "; added" in first.output
        second = runner.invoke(cli, arguments)
        assert second.exit_code == 0 and "; skipped" in second.output

    def test_add_short_doc_runs_compiler(self, tmp_path, pdf_model, monkeypatch):
        from ultrakb.agent.compiler import compile_short_doc
        from ultrakb.application.knowledge_bases import get_kb_list
        from ultrakb.application.pages import read_page
        from ultrakb.application.views import view_scope

        kb = self._setup_kb(tmp_path)
        doc = tmp_path / "test.md"
        doc.write_text("# Hello")
        compiled = []

        async def observe_compile(doc_name, source_path, kb_dir, model, *args, **kwargs):
            compiled.append(
                (doc_name, source_path, kb_dir, source_path.read_text(encoding="utf-8"))
            )
            return await compile_short_doc(doc_name, source_path, kb_dir, model, *args, **kwargs)

        monkeypatch.setattr("ultrakb.agent.compiler.compile_short_doc", observe_compile)
        result = CliRunner().invoke(cli, ["--kb-dir", str(kb), "add", str(doc)])
        assert result.exit_code == 0 and "; added" in result.output
        saved = get_kb_list(kb)["documents"][0]
        scope = view_scope(kb, saved["view_id"])
        summary = read_page(kb, "summaries/test", scope=scope)
        assert summary.body.strip()
        assert "hello" in summary.body.casefold()
        assert len(compiled) == 1
        assert compiled[0][0] == "test"
        assert compiled[0][2] == kb
        assert compiled[0][3].strip() == "# Hello"
        assert saved["length_class"] == "short"

    def test_add_requires_path(self, tmp_path):
        kb_dir = self._setup_kb(tmp_path)
        runner = CliRunner()
        with patch("ultrakb.cli._find_kb_dir", return_value=kb_dir):
            result = runner.invoke(cli, ["add"])
            assert "Provide a PATH" in result.output
