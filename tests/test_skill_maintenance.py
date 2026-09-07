"""CLI-only Skill maintenance follows the same complete mutation boundaries."""

import pytest
from click.testing import CliRunner

from openkb.cli import cli
from openkb.locks import kb_ingest_lock
from openkb.mutation import RecoveryRequired, repair_marker
from openkb.skill.evaluator import EvalPrompt, save_eval_set
from openkb.skill.workspace import save_iteration


def skill_history(kb_dir):
    skill = kb_dir / "output/skills/demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: earlier\n---\nEarlier")
    with kb_ingest_lock(kb_dir / ".openkb"):
        save_iteration(kb_dir, "demo")
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: current\n---\nCurrent")
    return skill


def test_cli_rollback_respects_repair_barrier(kb_dir):
    skill = skill_history(kb_dir)
    before = (skill / "SKILL.md").read_bytes()
    repair_marker(kb_dir).write_text("{}")
    result = CliRunner().invoke(cli, ["--kb-dir", str(kb_dir), "skill", "rollback", "demo", "-y"])
    assert isinstance(result.exception, RecoveryRequired)
    assert (skill / "SKILL.md").read_bytes() == before


def test_rollback_restores_current_and_archive_when_manifest_write_fails(kb_dir, monkeypatch):
    from openkb.skill import marketplace

    skill = skill_history(kb_dir)
    before = {
        path.relative_to(kb_dir): path.read_bytes()
        for path in (kb_dir / "output").rglob("*")
        if path.is_file()
    }

    def fail(*args, **kwargs):
        raise OSError("manifest storage failed")

    monkeypatch.setattr(marketplace, "regenerate_marketplace", fail)
    result = CliRunner().invoke(cli, ["--kb-dir", str(kb_dir), "skill", "rollback", "demo", "-y"])
    assert isinstance(result.exception, OSError)
    assert {
        path.relative_to(kb_dir): path.read_bytes()
        for path in (kb_dir / "output").rglob("*")
        if path.is_file()
    } == before
    assert "Current" in (skill / "SKILL.md").read_text()


def test_eval_set_save_refuses_repair_and_escaping_name(kb_dir):
    prompts = [EvalPrompt(question="Valid question", expected="trigger")]
    with pytest.raises(ValueError):
        save_eval_set(kb_dir, "../../escape", prompts)
    repair_marker(kb_dir).write_text("{}")
    with pytest.raises(RecoveryRequired):
        save_eval_set(kb_dir, "demo", prompts)
    assert not (kb_dir / ".openkb/eval-sets/demo.json").exists()


def test_eval_set_failure_restores_previous_prompts(kb_dir, monkeypatch):
    from openkb import locks

    target = save_eval_set(kb_dir, "demo", [EvalPrompt(question="Before", expected="trigger")])
    before = target.read_bytes()
    original = locks.atomic_write_text

    def fail(path, content, **kwargs):
        original(path, content, **kwargs)
        if path == target:
            raise OSError("write completed without transaction commit")

    monkeypatch.setattr(locks, "atomic_write_text", fail)
    with pytest.raises(OSError):
        save_eval_set(kb_dir, "demo", [EvalPrompt(question="After", expected="trigger")])
    assert target.read_bytes() == before
