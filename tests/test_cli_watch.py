"""CLI watch retains its entry-point semantics and stops unsafe subscriptions."""

import importlib
import os

import pytest
from click.testing import CliRunner

from ultrakb.application.knowledge_bases import initialize_kb
from ultrakb.application.sources import source_inventory
from ultrakb.mutation import RecoveryRequired

cli_module = importlib.import_module("ultrakb.cli")


@pytest.mark.parametrize("failure", ["repair", "replacement", "database-replacement"])
def test_cli_watch_stops_before_later_files_after_fatal_failure(kb_dir, monkeypatch, failure):
    import ultrakb.application.storage_compilation as compiler
    import ultrakb.watcher as watcher

    first = kb_dir / "raw/first.md"
    later = kb_dir / "raw/later.md"
    first.write_text("# First\n", encoding="utf-8")
    later.write_text("# Later\n", encoding="utf-8")
    calls = []

    def compile_document(*args, **kwargs):
        calls.append(args)
        raise RecoveryRequired(kb_dir)

    def observe(raw, callback, **kwargs):
        if failure == "replacement":
            old = kb_dir.with_name("previous")
            kb_dir.rename(old)
            initialize_kb(kb_dir, seed_environment=False)
        elif failure == "database-replacement":
            replacement = kb_dir.with_name("replacement")
            initialize_kb(replacement, seed_environment=False)
            os.replace(kb_dir / ".ultrakb", kb_dir.with_name("old-state"))
            os.replace(replacement / ".ultrakb", kb_dir / ".ultrakb")
        callback([str(first), str(later)])
        assert kwargs["cancelled"]()
        # Late delivery cannot revive a stopped subscription.
        callback([str(later)])

    monkeypatch.setattr(compiler, "materialize_inputs", compile_document)
    monkeypatch.setattr(watcher, "watch_directory", observe)
    result = CliRunner().invoke(cli_module.cli, ["--kb-dir", str(kb_dir), "watch"])
    assert result.exception is None, result.output
    assert "Watching stopped" in result.output
    assert len(calls) == (1 if failure == "repair" else 0)
    sources = source_inventory(kb_dir)
    assert len(sources) == (1 if failure == "repair" else 0)
    assert all(not item["name"].endswith("later.md") for item in sources)
