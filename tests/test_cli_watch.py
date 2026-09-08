"""CLI watch retains its entry-point semantics and stops unsafe subscriptions."""

import importlib
import shutil

import pytest
from click.testing import CliRunner

from openkb.mutation import RecoveryRequired
from openkb.state import HashRegistry

cli_module = importlib.import_module("openkb.cli")


@pytest.mark.parametrize("failure", ["repair", "replacement"])
def test_cli_watch_stops_before_later_files_after_fatal_failure(kb_dir, monkeypatch, failure):
    import openkb.agent.compiler as compiler
    import openkb.watcher as watcher

    first = kb_dir / "raw/first.md"
    later = kb_dir / "raw/later.md"
    first.write_text("# First\n", encoding="utf-8")
    later.write_text("# Later\n", encoding="utf-8")
    calls = []

    async def compile_document(*args, **kwargs):
        calls.append(args)
        raise RecoveryRequired(kb_dir)

    def observe(raw, callback, **kwargs):
        if failure == "replacement":
            old = kb_dir.with_name("previous")
            kb_dir.rename(old)
            shutil.copytree(old, kb_dir)
        callback([str(first), str(later)])
        assert kwargs["cancelled"]()
        # Late delivery cannot revive a stopped subscription.
        callback([str(later)])

    monkeypatch.setattr(compiler, "compile_short_doc", compile_document)
    monkeypatch.setattr(watcher, "watch_directory", observe)
    result = CliRunner().invoke(cli_module.cli, ["--kb-dir", str(kb_dir), "watch"])
    assert result.exception is None, result.output
    assert "Watching stopped" in result.output
    assert len(calls) == (1 if failure == "repair" else 0)
    assert HashRegistry(kb_dir / ".openkb/hashes.json").all_entries() == {}
