"""Regression tests for ``ultrakb.agent.tools.write_kb_file``.

Covers the allow-list (``wiki/explorations/**`` and ``output/**``), path
traversal rejection, the bare-directory guard (e.g. ``"output"`` alone),
and automatic parent-directory creation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ultrakb.agent.tools import write_kb_file as write_tool


@pytest.fixture
def kb_root(tmp_path: Path):
    from kb_fixtures import mark_current_kb

    from ultrakb.application.storage_model_outputs import storage_model_output_scope
    from ultrakb.application.views import published_scope
    from ultrakb.model_outputs import ModelOutputs
    from ultrakb.storage.execution_store import ExecutionStore

    mark_current_kb(tmp_path)
    scope = published_scope(tmp_path)
    executions = ExecutionStore(tmp_path, scope.knowledge.identity)
    execution = executions.begin("output-paths", fingerprint="a" * 64, max_calls=0)
    return tmp_path, scope, executions, execution, ModelOutputs, storage_model_output_scope


@pytest.fixture
def write_kb_file(kb_root):
    root, scope, executions, execution, outputs, output_scope = kb_root
    try:
        with output_scope(
            scope,
            outputs(),
            check_cancelled=lambda: None,
            execution_id=execution.execution_id,
        ):
            yield lambda path, content, _root: write_tool(path, content, str(root), scope=scope)
    finally:
        executions.finish(execution.execution_id, state="completed", work_settled=True)


def test_rejects_empty_path(kb_root, write_kb_file) -> None:
    result = write_kb_file("", "hi", kb_root)
    assert result.startswith("Access denied")


def test_rejects_bare_output_directory(kb_root, write_kb_file) -> None:
    # "output" alone resolves to the directory itself and would crash
    # write_text with IsADirectoryError. The guard must reject it.
    result = write_kb_file("output", "hi", kb_root)
    assert result.startswith("Access denied")
    # And, critically, no file/dir should have been written at that name.
    assert not (kb_root[0] / "output").exists() or (kb_root[0] / "output").is_dir()


def test_rejects_bare_wiki_explorations_directory(kb_root, write_kb_file) -> None:
    result = write_kb_file("wiki/explorations", "hi", kb_root)
    assert result.startswith("Access denied")


def test_rejects_outside_allow_list(kb_root, write_kb_file) -> None:
    result = write_kb_file("wiki/summaries/x.md", "hi", kb_root)
    assert result.startswith("Access denied")
    assert not (kb_root[0] / "wiki" / "summaries" / "x.md").exists()


def test_accepts_output_skill_path(kb_root, write_kb_file) -> None:
    result = write_kb_file("output/skills/demo/SKILL.md", "# demo\n", kb_root)
    assert result == "Written: output/skills/demo/SKILL.md"
    written = kb_root[0] / "output" / "skills" / "demo" / "SKILL.md"
    assert written.read_text(encoding="utf-8") == "# demo\n"


def test_accepts_wiki_exploration(kb_root, write_kb_file) -> None:
    result = write_kb_file("wiki/explorations/transcript.md", "notes", kb_root)
    assert result == "Written: wiki/explorations/transcript.md"
    written = kb_root[0] / "wiki" / "explorations" / "transcript.md"
    assert written.read_text(encoding="utf-8") == "notes"


def test_rejects_path_traversal(kb_root, write_kb_file) -> None:
    result = write_kb_file("../escape.md", "evil", kb_root)
    assert result.startswith("Access denied")
    # Must not have written outside the KB root.
    assert not (kb_root[0].parent / "escape.md").exists()


def test_rejects_traversal_via_allowed_prefix(kb_root, write_kb_file) -> None:
    # Even when starting with an allowed-looking prefix, traversal must escape
    # the KB root (Path.resolve normalizes ``..``) and be rejected.
    result = write_kb_file("output/../../escape.md", "evil", kb_root)
    assert result.startswith("Access denied")


def test_creates_parent_directories(kb_root, write_kb_file) -> None:
    # Deeply nested target with no pre-existing parents.
    target = "output/skills/new/nested/deep/SKILL.md"
    result = write_kb_file(target, "deep", kb_root)
    assert result == f"Written: {target}"
    assert (kb_root[0] / target).read_text(encoding="utf-8") == "deep"
