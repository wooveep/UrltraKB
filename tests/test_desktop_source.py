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
        "assets/fonts/SourceCodePro-Regular.ttf": "committed font fixture",
        "assets/private.txt": "private asset",
        "openkb/web/index.html": "retired browser bundle",
        "pyproject.toml": "[project]\nname = 'openkb'\n",
        "LICENSE": "original license",
        "Makefile": "help:\n\t@echo build\n",
        "docs/ocr-and-images.md": "OCR setup guide",
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
    assert (output / "Makefile").read_text("utf-8") == "help:\n\t@echo build\n"
    assert (output / "docs/ocr-and-images.md").read_text("utf-8") == "OCR setup guide"
    assert (
        output / "assets/fonts/SourceCodePro-Regular.ttf"
    ).read_text() == "committed font fixture"
    for name in (
        "CLAUDE.md",
        ".env",
        "docs/internal",
        "openkb/web",
        "openkb/untracked.py",
        "assets/private.txt",
    ):
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


@pytest.mark.parametrize("system", ["Windows", "Linux"])
def test_delivery_separates_source_from_branded_runtime(tmp_path, system):
    import hashlib
    import tarfile
    import zipfile
    from dataclasses import asdict

    from openkb.distribution import read_distribution
    from scripts.export_desktop_source import export_source
    from scripts.package_desktop import package_materials, package_runtime, record

    source = tmp_path / "export"
    identity = export_source(_repository(tmp_path), source)
    materials = tmp_path / "complete-materials"
    materials.mkdir()
    material_files = []
    for kind in ("source", "licenses", "notice", "components", "build"):
        path = materials / f"UrltraKB-{kind}.txt"
        path.write_text(f"Complete {kind} material", encoding="utf-8")
        material_files.append(asdict(record(path, kind)))
    (materials / "release.json").write_text(
        json.dumps({"schema": 1, **identity, "files": material_files}), encoding="utf-8"
    )
    program = tmp_path / "program"
    (program / "_internal/openkb").mkdir(parents=True)
    suffix = ".exe" if system == "Windows" else ""
    for name in ("UrltraKB", "UrltraKBCLI", "UrltraKBAPI", "UrltraKBVerify"):
        (program / (name + suffix)).write_bytes(b"inventoried executable fixture")
        (program / (name + suffix)).chmod(0o755)
    (program / "_internal/openkb/_build_info.json").write_text(json.dumps(identity))
    if system == "Linux":
        (program / "_internal/library.so.1").write_bytes(b"shared runtime")
        (program / "_internal/library.so").symlink_to("library.so.1")
    rows = []
    for path in program.rglob("*"):
        if path.is_file():
            row = asdict(record(path, "build"))
            row["path"] = path.relative_to(program).as_posix()
            if path.is_symlink():
                row["link"] = path.readlink().as_posix()
            rows.append(row)
    inventory = {**identity, "platform": {"system": system, "machine": "x86_64"}, "files": rows}
    (program / "private.env").write_text("must never ship")
    output = tmp_path / "delivery"
    companion = package_materials(source, materials, output)
    runtime = package_runtime(
        source, program, inventory, materials, output / companion.name, output
    )
    assert runtime.name.startswith(f"UrltraKB-{identity['version']}-")
    with zipfile.ZipFile(output / companion.name) as archive:
        assert "UrltraKB/distribution/UrltraKB-source.txt" in archive.namelist()
    extracted = tmp_path / "extracted"
    if system == "Windows":
        with zipfile.ZipFile(output / runtime.name) as archive:
            archive.extractall(extracted)
    else:
        with tarfile.open(output / runtime.name) as archive:
            archive.extractall(extracted, filter="data")
        assert (extracted / "UrltraKB/UrltraKB").stat().st_mode & 0o111
        assert (extracted / "UrltraKB/_internal/library.so").read_bytes() == b"shared runtime"
    release = read_distribution(extracted / "UrltraKB/distribution", identity)
    assert {f.kind for f in release.files} == {"licenses", "notice"}
    assert release.source_archive == companion
    assert not list(extracted.rglob("*source*"))
    assert not list(extracted.rglob("private.env"))
    assert not list(extracted.rglob("OpenKB*"))
    with release.open_file("UrltraKB-licenses.txt") as stream:
        assert stream.read() == b"Complete licenses material"
    import os

    for path in [*program.rglob("*"), *materials.iterdir()]:
        if path.is_file():
            os.utime(path, (946684800, 946684800))
    repeat = tmp_path / "repeat"
    repeated_companion = package_materials(source, materials, repeat)
    assert repeated_companion.sha256 == companion.sha256
    repeated_runtime = package_runtime(
        source, program, inventory, materials, repeat / companion.name, repeat
    )
    assert repeated_runtime.sha256 == runtime.sha256
    if system == "Linux":
        (program / "UrltraKB").chmod(0o644)
        with pytest.raises(ValueError, match="not executable"):
            package_runtime(
                source, program, inventory, materials, output / companion.name, tmp_path / "nonexec"
            )
        (program / "UrltraKB").chmod(0o755)
    (program / ("UrltraKB" + suffix)).write_bytes(b"changed executable")
    before = hashlib.sha256((output / runtime.name).read_bytes()).hexdigest()
    with pytest.raises(FileExistsError):
        package_runtime(source, program, inventory, materials, output / companion.name, output)
    assert hashlib.sha256((output / runtime.name).read_bytes()).hexdigest() == before
    with pytest.raises(ValueError, match="Program input changed"):
        package_runtime(
            source, program, inventory, materials, output / companion.name, tmp_path / "bad"
        )
    assert not list((tmp_path / "bad").iterdir())
