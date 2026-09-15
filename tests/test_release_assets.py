"""Publication requires matching, intact installers from every supported target."""

import hashlib
import json

import pytest

from scripts.prepare_release_assets import TARGETS, collect


def _artifacts(tmp_path):
    artifacts = tmp_path / "artifacts"
    for target, extension in TARGETS.items():
        folder = artifacts / target / "commit"
        folder.mkdir(parents=True)
        stem = f"UrltraKB-1.0.0-{target}"
        receipt = {"version": "1.0.0", "commit": "a" * 40, "target": target}
        for suffix, key in [(extension, "installer_sha256"), ("-source.zip", "source_sha256")]:
            content = (target + suffix).encode()
            (folder / (stem + suffix)).write_bytes(content)
            receipt[key] = hashlib.sha256(content).hexdigest()
        (folder / (stem + "-build.json")).write_text(json.dumps(receipt))
    return artifacts


def test_release_collects_four_installers_with_sources_and_verified_checksums(tmp_path):
    artifacts = _artifacts(tmp_path)
    output = tmp_path / "release"
    collect(artifacts, output, "v1.0.0", "a" * 40)
    assert len(list(output.iterdir())) == 13
    for row in (output / "SHA256SUMS.txt").read_text().splitlines():
        checksum, name = row.split("  ")
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == checksum
    assert len(list(output.glob("*-build.json"))) == 4


@pytest.mark.parametrize("defect", ["missing", "corrupt", "commit", "version", "duplicate"])
def test_release_refuses_incomplete_or_mixed_builds(tmp_path, defect):
    artifacts = _artifacts(tmp_path)
    path = next(artifacts.rglob("*-build.json"))
    receipt = json.loads(path.read_text())
    if defect == "missing":
        path.unlink()
    elif defect == "corrupt":
        path.with_name(path.name.replace("-build.json", "-source.zip")).write_bytes(b"changed")
    elif defect == "duplicate":
        (artifacts / path.name).write_text(path.read_text())
    else:
        receipt[defect] = "b" * 40 if defect == "commit" else "0.1.dev1+g123456789abc"
        path.write_text(json.dumps(receipt))
    output = tmp_path / "release"
    with pytest.raises(ValueError):
        collect(artifacts, output, "v1.0.0", "a" * 40)
    assert not output.exists()
