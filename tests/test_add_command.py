"""Tests for the `add` CLI command (Task 10)."""

from __future__ import annotations

import json
from unittest.mock import patch

from click.testing import CliRunner

from openkb.cli import SUPPORTED_EXTENSIONS, _find_kb_dir, cli

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

    def test_add_single_file_calls_helper(self, tmp_path):
        kb_dir = self._setup_kb(tmp_path)
        doc = tmp_path / "test.md"
        doc.write_text("# Hello")

        runner = CliRunner()
        with (
            patch("openkb.cli.add_single_file") as mock_add,
            patch("openkb.cli._find_kb_dir", return_value=kb_dir),
        ):
            runner.invoke(cli, ["add", str(doc)])
            mock_add.assert_called_once_with(doc, kb_dir, scope=None, metadata=None)

    def test_add_single_file_compile_failure_rolls_back_converted_artifacts(self, tmp_path):
        from openkb.cli import add_single_file
        from openkb.state import HashRegistry

        kb_dir = self._setup_kb(tmp_path)
        doc = tmp_path / "notes.md"
        doc.write_text("# Notes\n\nBody", encoding="utf-8")

        with (
            patch("openkb.agent.compiler.compile_short_doc", side_effect=RuntimeError("boom")),
            patch("openkb.application.documents.time.sleep"),
            patch("openkb.cli._setup_llm_key"),
        ):
            outcome = add_single_file(doc, kb_dir)

        assert outcome == "failed"
        assert not (kb_dir / "raw" / "notes.md").exists()
        assert not (kb_dir / "wiki" / "sources" / "notes.md").exists()
        assert HashRegistry(kb_dir / ".openkb" / "hashes.json").all_entries() == {}

    def test_add_forwards_concurrency_from_config(self, tmp_path):
        from unittest.mock import AsyncMock

        from openkb.cli import add_single_file

        kb_dir = self._setup_kb(tmp_path)
        (kb_dir / ".openkb" / "config.yaml").write_text(
            "model: gpt-4o-mini\nconcurrency: 3\n", encoding="utf-8"
        )
        doc = tmp_path / "notes.md"
        doc.write_text("# Notes\n\nBody", encoding="utf-8")

        with (
            patch(
                "openkb.agent.compiler.compile_short_doc", new_callable=AsyncMock
            ) as mock_compile,
            patch("openkb.cli._setup_llm_key"),
        ):
            outcome = add_single_file(doc, kb_dir)

        assert outcome == "added"
        assert mock_compile.call_args.kwargs["max_concurrency"] == 3

    def test_add_single_file_publishes_an_immutable_source(self, tmp_path, pdf_model):
        from openkb.application.knowledge_bases import get_kb_list
        from openkb.cli import add_single_file
        from openkb.documents import read_document_source

        kb = self._setup_kb(tmp_path)
        doc = tmp_path / "coordinated.md"
        doc.write_text("# Coordinated\n", encoding="utf-8")
        assert add_single_file(doc, kb) == "added"
        saved = read_document_source(kb, get_kb_list(kb)["documents"][0]["hash"])
        assert saved["content"] == "# Coordinated\n"
        assert saved["knowledge_revision_id"] is not None

    def _long_doc_conv(self, kb_dir, name, file_hash):
        from openkb.converter import ConvertResult

        return ConvertResult(
            raw_path=kb_dir / "raw" / f"{name}.pdf",
            source_path=None,
            is_long_doc=True,
            file_hash=file_hash,
            doc_name=name,
        )

    def test_long_doc_rollback_removes_only_the_new_blob(self, tmp_path):
        """A failed long-doc add must roll back the blob IT created under
        .openkb/files, while a pre-existing blob (another document) survives —
        the targeted track_new must not touch blobs this add didn't create."""
        from openkb.cli import add_single_file
        from openkb.indexer import IndexResult

        kb_dir = self._setup_kb(tmp_path)
        files = kb_dir / ".openkb" / "files" / "default"
        files.mkdir(parents=True)
        other = files / "other-doc.pdf"
        other.write_bytes(b"another-doc-keep-me")

        new_id = "11111111-1111-1111-1111-111111111111"

        def fake_index(raw_path, kb_dir_arg, doc_name=None):
            (files / f"{new_id}.pdf").write_bytes(b"new-blob")
            (files / new_id / "images").mkdir(parents=True)
            (files / new_id / "images" / "p1.png").write_bytes(b"img")
            return IndexResult(doc_id=new_id, description="", tree={"structure": []})

        doc = tmp_path / "paper.pdf"
        doc.write_bytes(b"%PDF-1.4 fake")
        conv = self._long_doc_conv(kb_dir, "paper", "cafebabe00" * 8)

        with (
            patch("openkb.application.documents.convert_document", return_value=conv),
            patch("openkb.indexer.index_long_document", side_effect=fake_index),
            patch("openkb.agent.compiler.compile_long_doc", side_effect=RuntimeError("boom")),
            patch("openkb.application.documents.time.sleep"),
            patch("openkb.cli._setup_llm_key"),
        ):
            outcome = add_single_file(doc, kb_dir)

        assert outcome == "failed"
        assert not (files / f"{new_id}.pdf").exists()  # new blob rolled back
        assert not (files / new_id).exists()  # new images subtree rolled back
        assert other.read_bytes() == b"another-doc-keep-me"  # pre-existing survives

    def test_long_doc_dedup_hit_does_not_delete_existing_blob(self, tmp_path):
        """PageIndex content-dedup can return an EXISTING doc_id and write no new
        blob (diverged hashes.json/pageindex.db). A failed add must NOT delete
        that pre-existing blob on rollback (regression: track_new globbing the
        doc_id would otherwise register and delete it)."""
        from openkb.cli import add_single_file
        from openkb.indexer import IndexResult

        kb_dir = self._setup_kb(tmp_path)
        files = kb_dir / ".openkb" / "files" / "default"
        files.mkdir(parents=True)
        existing_id = "22222222-2222-2222-2222-222222222222"
        existing_blob = files / f"{existing_id}.pdf"
        existing_blob.write_bytes(b"pre-existing-do-not-delete")

        def fake_index_dedup(raw_path, kb_dir_arg, doc_name=None):
            # Dedup hit: return the existing doc_id, create NO new blob.
            return IndexResult(doc_id=existing_id, description="", tree={"structure": []})

        doc = tmp_path / "dup.pdf"
        doc.write_bytes(b"%PDF-1.4 dup")
        conv = self._long_doc_conv(kb_dir, "dup", "feedface00" * 8)

        with (
            patch("openkb.application.documents.convert_document", return_value=conv),
            patch("openkb.indexer.index_long_document", side_effect=fake_index_dedup),
            patch("openkb.agent.compiler.compile_long_doc", side_effect=RuntimeError("boom")),
            patch("openkb.application.documents.time.sleep"),
            patch("openkb.cli._setup_llm_key"),
        ):
            outcome = add_single_file(doc, kb_dir)

        assert outcome == "failed"
        assert existing_blob.read_bytes() == b"pre-existing-do-not-delete"

    def test_add_directory_calls_helper_for_each_file(self, tmp_path):
        kb_dir = self._setup_kb(tmp_path)
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        (docs_dir / "a.md").write_text("# A")
        (docs_dir / "b.txt").write_text("B content")
        (docs_dir / "ignore.xyz").write_text("skip me")

        runner = CliRunner()
        with (
            patch("openkb.cli.add_single_file") as mock_add,
            patch("openkb.cli._find_kb_dir", return_value=kb_dir),
        ):
            runner.invoke(cli, ["add", str(docs_dir)])
            # Should be called for .md and .txt but not .xyz
            assert mock_add.call_count == 2
            called_names = {call.args[0].name for call in mock_add.call_args_list}
            assert "a.md" in called_names
            assert "b.txt" in called_names
            assert "ignore.xyz" not in called_names

    def test_add_directory_stops_after_dirty_rollback(self, tmp_path):
        import pytest

        from openkb.add_coordinator import DirtyRollbackError

        kb_dir = self._setup_kb(tmp_path)
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        (docs_dir / "a.md").write_text("# A")
        (docs_dir / "b.md").write_text("# B")
        dirty_error = DirtyRollbackError(
            "add",
            kb_dir / ".openkb" / "journal" / "retained.json",
        )

        runner = CliRunner()
        with (
            patch("openkb.cli.add_single_file", side_effect=dirty_error) as mock_add,
            patch("openkb.cli._find_kb_dir", return_value=kb_dir),
        ):
            with pytest.raises(DirtyRollbackError) as exc_info:
                runner.invoke(cli, ["add", str(docs_dir)], catch_exceptions=False)

        assert exc_info.value is dirty_error
        mock_add.assert_called_once()
        assert mock_add.call_args.args[0].name == "a.md"

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

    def test_add_short_doc_runs_compiler(self, tmp_path, pdf_model):
        from openkb.application.knowledge_bases import get_kb_list
        from openkb.application.pages import read_page
        from openkb.application.views import view_scope

        kb = self._setup_kb(tmp_path)
        doc = tmp_path / "test.md"
        doc.write_text("# Hello")
        result = CliRunner().invoke(cli, ["--kb-dir", str(kb), "add", str(doc)])
        assert result.exit_code == 0 and "; added" in result.output
        saved = get_kb_list(kb)["documents"][0]
        scope = view_scope(kb, saved["view_id"])
        assert "First and last sections." in read_page(kb, "summaries/test", scope=scope).body
        assert saved["length_class"] == "short"

    def test_new_markdown_preserves_an_unmapped_legacy_entry(self, tmp_path, pdf_model):
        from openkb.application.knowledge_bases import get_kb_list
        from openkb.state import HashRegistry

        kb = self._setup_kb(tmp_path)
        HashRegistry(kb / ".openkb/hashes.json").add("old-hash", {"name": "notes.md", "type": "md"})
        doc = tmp_path / "notes.md"
        doc.write_text("# Notes, edited")
        result = CliRunner().invoke(cli, ["--kb-dir", str(kb), "add", str(doc)])
        assert result.exit_code == 0 and "; added" in result.output
        documents = get_kb_list(kb)["documents"]
        assert len(documents) == 2
        assert {bool(item.get("source_id")) for item in documents} == {False, True}

    def test_add_requires_path(self, tmp_path):
        kb_dir = self._setup_kb(tmp_path)
        runner = CliRunner()
        with patch("openkb.cli._find_kb_dir", return_value=kb_dir):
            result = runner.invoke(cli, ["add"])
            assert "Provide a PATH" in result.output


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
