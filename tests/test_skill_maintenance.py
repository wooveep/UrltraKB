"""CLI-only Skill maintenance follows the same complete mutation boundaries."""

import pytest
from click.testing import CliRunner

from ultrakb.cli import cli
from ultrakb.mutation import RecoveryRequired, repair_marker
from ultrakb.skill.evaluator import EvalPrompt, save_eval_set


def skill_history(kb_dir):
    skill = kb_dir / "output/skills/demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: earlier\n---\nEarlier")
    archive = kb_dir / "output/skills/demo-workspace/iteration-1"
    archive.mkdir(parents=True)
    (archive / "SKILL.md").write_bytes((skill / "SKILL.md").read_bytes())
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: current\n---\nCurrent")
    from artifact_fixtures import publish_artifacts

    publish_artifacts(kb_dir, kb_dir / "output", sync=True)
    return skill


def test_cli_rollback_refuses_an_unreadable_master(kb_dir):
    skill = skill_history(kb_dir)
    before = (skill / "SKILL.md").read_bytes()
    from ultrakb.storage.database import StorageFormatError, database_path

    database_path(kb_dir).write_bytes(b"unsupported-master")
    result = CliRunner().invoke(cli, ["--kb-dir", str(kb_dir), "skill", "rollback", "demo", "-y"])
    assert isinstance(result.exception, StorageFormatError)
    assert (skill / "SKILL.md").read_bytes() == before


def test_rollback_keeps_master_and_archive_when_manifest_projection_fails(kb_dir, monkeypatch):
    import ultrakb.storage.artifact_store as artifact_io
    from ultrakb.application.views import published_scope
    from ultrakb.storage.artifact_store import ArtifactStore

    skill = skill_history(kb_dir)
    scope = published_scope(kb_dir)
    original = artifact_io.atomic_write_bytes

    def fail(path, *args, **kwargs):
        if path == kb_dir / ".claude-plugin/marketplace.json":
            raise OSError("manifest output unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(artifact_io, "atomic_write_bytes", fail)
    result = CliRunner().invoke(cli, ["--kb-dir", str(kb_dir), "skill", "rollback", "demo", "-y"])
    assert isinstance(result.exception, RecoveryRequired)
    artifacts = ArtifactStore(kb_dir, scope.knowledge.identity)
    assert b"Earlier" in artifacts.read("output/skills/demo/SKILL.md")
    assert b"Current" in artifacts.read("output/skills/demo-workspace/iteration-2/SKILL.md")
    assert b"Earlier" in (skill / "SKILL.md").read_bytes()
    assert published_scope(kb_dir).publication_id == scope.publication_id


def test_eval_set_save_refuses_repair_and_escaping_name(kb_dir):
    prompts = [EvalPrompt(question="Valid question", expected="trigger")]
    with pytest.raises(ValueError):
        save_eval_set(kb_dir, "../../escape", prompts)
    repair_marker(kb_dir).write_text("{}")
    with pytest.raises(RecoveryRequired):
        save_eval_set(kb_dir, "demo", prompts)
    assert not (kb_dir / ".ultrakb/eval-sets/demo.json").exists()


def test_eval_set_failure_restores_previous_prompts(kb_dir, monkeypatch):
    from ultrakb import locks

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
