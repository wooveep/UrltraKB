"""Tests for the `add` CLI command (Task 10)."""

from __future__ import annotations

import json
from unittest.mock import patch

from click.testing import CliRunner
from processing_fixtures import configure_processing

from openkb.cli import SUPPORTED_EXTENSIONS, _find_kb_dir, add_single_file, cli


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
    def test_finds_openkb_dir(self, tmp_path, monkeypatch):
        (tmp_path / ".openkb").mkdir()
        (tmp_path / ".openkb/config.yaml").write_text("{}")
        monkeypatch.chdir(tmp_path)
        result = _find_kb_dir()
        assert result is not None

    def test_returns_none_if_no_openkb(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with patch("openkb.cli.load_global_config", return_value={}):
            result = _find_kb_dir()
            assert result is None

    def test_lifecycle_metadata_is_not_an_initialized_knowledge_base(self, tmp_path, monkeypatch):
        (tmp_path / ".openkb/kb-lifecycle").mkdir(parents=True)
        nested = tmp_path / "project"
        nested.mkdir()
        monkeypatch.chdir(nested)
        with patch("openkb.cli.load_global_config", return_value={}):
            result = CliRunner().invoke(cli, ["status"])
        assert "No knowledge base found" in result.output

    def test_damaged_current_kb_does_not_fall_back_to_another_default(self, tmp_path, monkeypatch):
        from openkb.mutation import RecoveryRequired

        damaged, other = tmp_path / "damaged", tmp_path / "other"
        for root in (damaged, other):
            (root / ".openkb").mkdir(parents=True)
            (root / "wiki").mkdir()
        (damaged / ".openkb/needs-repair.json").write_text("{}")
        (other / ".openkb/config.yaml").write_text("{}")
        monkeypatch.chdir(damaged)
        with patch("openkb.cli.load_global_config", return_value={"default_kb": str(other)}):
            result = CliRunner().invoke(cli, ["status"])
        assert isinstance(result.exception, RecoveryRequired)
        assert str(damaged) in str(result.exception)


class TestAddCommand:
    def _setup_kb(self, tmp_path):
        """Create a minimal KB structure."""
        (tmp_path / "raw").mkdir()
        (tmp_path / "wiki" / "sources" / "images").mkdir(parents=True)
        (tmp_path / "wiki" / "summaries").mkdir(parents=True)
        (tmp_path / "wiki" / "concepts").mkdir(parents=True)
        (tmp_path / "wiki" / "reports").mkdir(parents=True)
        openkb_dir = tmp_path / ".openkb"
        openkb_dir.mkdir()
        (openkb_dir / "config.yaml").write_text("model: gpt-4o-mini\n")
        configure_processing(tmp_path)
        (openkb_dir / "hashes.json").write_text(json.dumps({}))
        return tmp_path

    def test_add_missing_init(self, tmp_path):
        runner = CliRunner()
        with (
            runner.isolated_filesystem(temp_dir=tmp_path),
            patch("openkb.cli._find_kb_dir", return_value=None),
        ):
            result = runner.invoke(cli, ["add", "somefile.pdf"])
            assert "No knowledge base found" in result.output

    def test_add_single_file_runs_isolated_task(self, kb_dir, tmp_path, monkeypatch, model_service):
        doc = tmp_path / "test.md"
        doc.write_text("# Hello")
        monkeypatch.setattr("openkb.config.GLOBAL_CONFIG_DIR", tmp_path / "config")
        monkeypatch.setattr("openkb.cli._find_kb_dir", lambda *args: kb_dir)
        result = CliRunner().invoke(cli, ["add", str(doc)])
        assert result.exit_code == 0, result.output
        assert "compilation=completed" in result.output
        assert len(list((kb_dir / "wiki/summaries").glob("test-*.md"))) == 1
        assert len(model_service) == 4

    def test_add_single_file_compile_failure_rolls_back_converted_artifacts(self, tmp_path):
        from openkb.state import HashRegistry

        kb_dir = self._setup_kb(tmp_path)
        doc = tmp_path / "notes.md"
        doc.write_text("# Notes\n\nBody", encoding="utf-8")

        with (
            patch("openkb.agent.compiler.compile_short_doc", side_effect=RuntimeError("boom")),
            patch("openkb.cli._setup_llm_key"),
        ):
            outcome = add_single_file(doc, kb_dir)

        assert outcome == "failed"
        assert not (kb_dir / "raw" / "notes.md").exists()
        assert not (kb_dir / "wiki" / "sources" / "notes.md").exists()
        assert HashRegistry(kb_dir / ".openkb" / "hashes.json").all_entries() == {}

    def test_add_directory_processes_supported_files(
        self, kb_dir, tmp_path, monkeypatch, model_service
    ):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "a.md").write_text("# A")
        (docs / "b.md").write_text("# B")
        (docs / "ignore.xyz").write_text("ignored")
        monkeypatch.setattr("openkb.config.GLOBAL_CONFIG_DIR", tmp_path / "config")
        monkeypatch.setattr("openkb.cli._find_kb_dir", lambda *args: kb_dir)
        result = CliRunner().invoke(cli, ["add", str(docs)])
        assert result.exit_code == 0, result.output
        assert "2 document(s)" in result.output
        from openkb.sources import SourceStore

        store = SourceStore(kb_dir)
        assert {v.name for v in store.list_sources()} == {"a.md", "b.md"}
        assert {store.original(v).read_text() for v in store.list_sources()} == {"# A", "# B"}
        assert len(model_service) == 8

    def test_add_directory_stops_when_recovery_is_required(
        self, kb_dir, tmp_path, monkeypatch, model_service
    ):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "a.md").write_text("# A")
        (docs / "b.md").write_text("# B")
        (kb_dir / ".openkb/needs-repair.json").write_text("{}")
        monkeypatch.setattr("openkb.config.GLOBAL_CONFIG_DIR", tmp_path / "config")
        monkeypatch.setattr("openkb.cli._find_kb_dir", lambda *args: kb_dir)
        result = CliRunner().invoke(cli, ["add", str(docs)])
        assert result.exit_code == 1, result.output
        assert not list((kb_dir / "wiki/summaries").iterdir())
        assert model_service == []

    def test_add_unsupported_extension(self, tmp_path):
        kb_dir = self._setup_kb(tmp_path)
        doc = tmp_path / "file.xyz"
        doc.write_text("content")

        runner = CliRunner()
        with patch("openkb.cli._find_kb_dir", return_value=kb_dir):
            result = runner.invoke(cli, ["add", str(doc)])
            assert "Unsupported file type" in result.output

    def test_add_nonexistent_path(self, tmp_path):
        kb_dir = self._setup_kb(tmp_path)

        runner = CliRunner()
        with patch("openkb.cli._find_kb_dir", return_value=kb_dir):
            result = runner.invoke(cli, ["add", str(tmp_path / "nonexistent.pdf")])
            assert "does not exist" in result.output

    def test_add_skipped_file(self, kb_dir, tmp_path, monkeypatch, model_service):
        doc = tmp_path / "test.md"
        doc.write_text("# Hello")
        monkeypatch.setattr("openkb.config.GLOBAL_CONFIG_DIR", tmp_path / "config")
        monkeypatch.setattr("openkb.cli._find_kb_dir", lambda *args: kb_dir)
        runner = CliRunner()
        assert runner.invoke(cli, ["add", str(doc)]).exit_code == 0
        result = runner.invoke(cli, ["add", str(doc)])
        assert result.exit_code == 0, result.output
        assert "SKIP" in result.output
        assert len(model_service) == 4


class TestAddMutationCoordinator:
    def _setup_kb(self, tmp_path):
        (tmp_path / "raw").mkdir()
        (tmp_path / "wiki" / "sources" / "images").mkdir(parents=True)
        (tmp_path / "wiki" / "summaries").mkdir(parents=True)
        (tmp_path / "wiki" / "concepts").mkdir(parents=True)
        (tmp_path / "wiki" / "entities").mkdir(parents=True)
        openkb_dir = tmp_path / ".openkb"
        openkb_dir.mkdir()
        (openkb_dir / "config.yaml").write_text("model: gpt-4o-mini\n", encoding="utf-8")
        (openkb_dir / "hashes.json").write_text("{}", encoding="utf-8")
        return tmp_path

    def test_coordinator_rolls_back_before_commit_and_skips_post_commit(self, tmp_path):
        from openkb.add_coordinator import AddMutationPlan, run_add_mutation
        from openkb.locks import kb_ingest_lock

        kb_dir = self._setup_kb(tmp_path)
        official = kb_dir / "wiki" / "sources" / "doc.md"
        post_commit_calls = []

        def body(_snapshot):
            official.parent.mkdir(parents=True, exist_ok=True)
            official.write_text("# changed\n", encoding="utf-8")
            raise RuntimeError("before commit")

        plan = AddMutationPlan(
            operation="add",
            details={"doc_name": "doc"},
            touched_paths=[official],
            body=body,
            post_commit_hooks=[lambda: post_commit_calls.append("ran")],
        )

        with kb_ingest_lock(kb_dir / ".openkb"):
            assert run_add_mutation(kb_dir, plan) is False
        assert not official.exists()
        assert post_commit_calls == []

    def test_coordinator_reports_failed_mutation(self, tmp_path, capsys):
        from openkb.add_coordinator import AddMutationPlan, run_add_mutation
        from openkb.locks import kb_ingest_lock

        kb_dir = self._setup_kb(tmp_path)
        official = kb_dir / "wiki" / "sources" / "doc.md"

        def body(_snapshot):
            official.parent.mkdir(parents=True, exist_ok=True)
            official.write_text("# changed\n", encoding="utf-8")
            raise RuntimeError("boom")

        plan = AddMutationPlan(
            operation="add",
            details={"name": "doc.md", "doc_name": "doc"},
            touched_paths=[official],
            body=body,
        )

        with kb_ingest_lock(kb_dir / ".openkb"):
            assert run_add_mutation(kb_dir, plan) is False

        output = capsys.readouterr().out
        assert "[ERROR] add failed for doc.md: boom" in output

    def test_coordinator_post_commit_failure_does_not_roll_back(self, tmp_path):
        from openkb.add_coordinator import AddMutationPlan, run_add_mutation
        from openkb.locks import kb_ingest_lock

        kb_dir = self._setup_kb(tmp_path)
        official = kb_dir / "wiki" / "sources" / "doc.md"

        def body(_snapshot):
            official.parent.mkdir(parents=True, exist_ok=True)
            official.write_text("# committed\n", encoding="utf-8")

        def bad_hook():
            raise RuntimeError("hook failed")

        plan = AddMutationPlan(
            operation="add",
            details={"doc_name": "doc"},
            touched_paths=[official],
            body=body,
            post_commit_hooks=[bad_hook],
        )

        with kb_ingest_lock(kb_dir / ".openkb"):
            assert run_add_mutation(kb_dir, plan) is True
        assert official.read_text(encoding="utf-8") == "# committed\n"
        assert list((kb_dir / ".openkb" / "journal").glob("*.json")) == []

    def test_coordinator_keyboard_interrupt_rolls_back_and_reraises(self, tmp_path):
        import pytest

        from openkb.add_coordinator import AddMutationPlan, run_add_mutation
        from openkb.locks import kb_ingest_lock

        kb_dir = self._setup_kb(tmp_path)
        official = kb_dir / "wiki" / "sources" / "doc.md"
        post_commit_calls = []

        def body(_snapshot):
            official.parent.mkdir(parents=True, exist_ok=True)
            official.write_text("# interrupted\n", encoding="utf-8")
            raise KeyboardInterrupt()

        plan = AddMutationPlan(
            operation="add",
            details={"doc_name": "doc"},
            touched_paths=[official],
            body=body,
            post_commit_hooks=[lambda: post_commit_calls.append("ran")],
        )

        with kb_ingest_lock(kb_dir / ".openkb"):
            with pytest.raises(KeyboardInterrupt):
                run_add_mutation(kb_dir, plan)

        assert not official.exists()
        assert post_commit_calls == []
        assert list((kb_dir / ".openkb" / "journal").glob("*.json")) == []

    def test_run_add_mutation_requires_lock_held(self, tmp_path):
        import pytest

        from openkb.add_coordinator import AddMutationPlan, run_add_mutation

        kb_dir = self._setup_kb(tmp_path)

        plan = AddMutationPlan(
            operation="add",
            details={"doc_name": "doc"},
            touched_paths=[kb_dir / "wiki" / "sources" / "doc.md"],
            body=lambda _snapshot: None,
        )

        with pytest.raises(RuntimeError, match="requires the caller to hold kb_ingest_lock"):
            run_add_mutation(kb_dir, plan)

    def test_run_add_mutation_raises_dirty_rollback_when_rollback_fails(self, tmp_path):
        import pytest

        from openkb.add_coordinator import AddMutationPlan, DirtyRollbackError, run_add_mutation
        from openkb.locks import kb_ingest_lock
        from openkb.mutation import MutationSnapshot

        kb_dir = self._setup_kb(tmp_path)
        official = kb_dir / "wiki" / "sources" / "doc.md"
        official.parent.mkdir(parents=True, exist_ok=True)
        official.write_text("# before\n", encoding="utf-8")

        def body(_snapshot):
            official.write_text("# after\n", encoding="utf-8")
            raise RuntimeError("body fails after partial apply")

        plan = AddMutationPlan(
            operation="add",
            details={"doc_name": "doc"},
            touched_paths=[official],
            body=body,
        )

        # Force rollback to fail: rollback_best_effort returns the error instead
        # of None, so the active journal is retained for next-run recovery.
        with kb_ingest_lock(kb_dir / ".openkb"):
            with patch.object(
                MutationSnapshot, "rollback_best_effort", return_value=OSError("disk full")
            ):
                with pytest.raises(DirtyRollbackError) as exc_info:
                    run_add_mutation(kb_dir, plan)

        assert exc_info.value.operation == "add"
        # The journal is retained on disk for next-run recovery.
        assert exc_info.value.journal_path.exists()
        assert exc_info.value.journal_path.is_file()
