"""Release exports bind the actual source and never sweep up workspace state."""

import json
import subprocess

import pytest


def _repository(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for name, text in {
        "openkb/example.py": "COMMITTED = True\n",
        "openkb/示例资源.txt": "已提交的中文资源\n",
        "openkb/version.txt": "$Format:%H$\n",
        "openkb/web/index.html": "retired browser bundle",
        "pyproject.toml": "[project]\nname = 'openkb'\n",
        "LICENSE": "original license",
        "CLAUDE.md": "private working notes",
        ".env": "PRIVATE_TEST_VALUE=do-not-export",
        "docs/internal/private.md": "private design history",
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Build test",
            "-c",
            "user.email=build-test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    return root


def test_source_export_uses_commit_and_excludes_private_or_retired_files(tmp_path):
    from scripts.export_desktop_source import export_source, verify_source

    repo = _repository(tmp_path)
    (repo / "openkb/example.py").write_text("UNCOMMITTED = True\n", encoding="utf-8")
    (repo / "openkb/untracked.py").write_text("private workspace data", encoding="utf-8")
    output = tmp_path / "export"
    identity = export_source(repo, output)
    assert (output / "openkb/example.py").read_text("utf-8") == "COMMITTED = True\n"
    assert (output / "openkb/示例资源.txt").read_text("utf-8") == "已提交的中文资源\n"
    for name in ("CLAUDE.md", ".env", "docs/internal", "openkb/web", "openkb/untracked.py"):
        assert not (output / name).exists()
    assert json.loads((output / "openkb/_build_info.json").read_text("utf-8")) == identity
    assert verify_source(output) == identity
    assert (
        identity["commit"]
        == subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    )


def test_build_refuses_modified_export_or_additional_application_source(tmp_path):
    from scripts.export_desktop_source import export_source, verify_source

    output = tmp_path / "export"
    export_source(_repository(tmp_path), output)
    source = output / "openkb/example.py"
    original = source.read_bytes()
    source.write_text("MODIFIED = True\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        verify_source(output)
    source.write_bytes(original)
    (output / "openkb/injected.py").write_text("EXTRA = True\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Unexpected"):
        verify_source(output)


def test_export_does_not_overwrite_an_existing_directory(tmp_path):
    from scripts.export_desktop_source import export_source

    output = tmp_path / "existing"
    output.mkdir()
    protected = output / "keep.txt"
    protected.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        export_source(_repository(tmp_path), output)
    assert protected.read_text("utf-8") == "keep"


def test_local_archive_attributes_cannot_remove_or_rewrite_source(tmp_path):
    from scripts.export_desktop_source import export_source, verify_source

    repo = _repository(tmp_path)
    (repo / ".git/info/attributes").write_text(
        "openkb/example.py export-ignore\nopenkb/version.txt export-subst\n", encoding="utf-8"
    )
    output = tmp_path / "export"
    export_source(repo, output)
    assert (output / "openkb/example.py").read_text("utf-8") == "COMMITTED = True\n"
    assert (output / "openkb/version.txt").read_text("utf-8") == "$Format:%H$\n"
    verify_source(output)


def test_local_replacement_refs_cannot_change_committed_blob_bytes(tmp_path):
    from scripts.export_desktop_source import export_source

    repo = _repository(tmp_path)
    original = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD:openkb/example.py"], text=True
    ).strip()
    replacement = (
        subprocess.check_output(
            ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
            input=b"LOCAL_REPLACEMENT = True\n",
        )
        .decode()
        .strip()
    )
    subprocess.run(["git", "-C", str(repo), "replace", original, replacement], check=True)
    output = tmp_path / "export"
    export_source(repo, output)
    assert (output / "openkb/example.py").read_text("utf-8") == "COMMITTED = True\n"
