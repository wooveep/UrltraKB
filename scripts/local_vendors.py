"""Verify package identity in installed releases and source checkouts separately."""

from __future__ import annotations

import hashlib
import json
from importlib import metadata, util
from pathlib import Path

# folder, distribution, module, local version, audited upstream commit
VENDORS = (
    (
        "PageIndex",
        "pageindex",
        "pageindex",
        "0.3.0.dev3+urltrakb.6",
        "9ad54122bbd519cec8913198e2d63cff92781c1e",
    ),
    (
        "ChatIndex",
        "ictree",
        "ctree",
        "0.1.0+urltrakb.2",
        "7df2c9208db6f113f85a6c09295bec7f0f2114e7",
    ),
    (
        "ConDB",
        "pageindex-condb",
        "contextdb",
        "1.0+urltrakb.1",
        "62da030426b3eee96a77b464e7007cdf8530c42e",
    ),
    (
        "LiteLLM",
        "litellm",
        "litellm",
        "1.87.2+urltrakb.2",
        "1296275dc52d9f4e05696380735037fbb841fcc3",
    ),
)


def verify_vendor_source(source: Path) -> int:
    """Check retained upstream bytes and declared local patches, including deletions."""
    provenance = json.loads((source / "UPSTREAM.json").read_text("utf-8"))
    patch = provenance["local_patch"]
    expected = dict(provenance["upstream_files"])
    for name in patch["deleted"]:
        expected.pop(name)
    expected.update(patch["modified"])
    expected.update(patch["added"])
    actual = {}
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if ".git" in relative.parts:
            raise ValueError(f"Nested Git metadata in vendor source: {relative}")
        if any(
            part in {"__pycache__", ".pytest_cache", "dist", "build"} or part.endswith(".egg-info")
            for part in relative.parts
        ):
            continue
        if path.is_file() and relative.as_posix() != "UPSTREAM.json":
            actual[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    changed = sorted(
        name for name in actual.keys() | expected.keys() if actual.get(name) != expected.get(name)
    )
    if changed:
        raise ValueError(f"Unrecorded vendor source changes in {source.name}: {', '.join(changed)}")
    return len(actual)


def verify_installed_vendors() -> dict[str, Path]:
    """Validate distribution versions and packaged provenance without importing SDKs."""
    verified = {}
    for _, distribution, module, version, commit in VENDORS:
        if metadata.version(distribution) != version:
            raise ValueError(f"{distribution} metadata does not match local version {version}")
        spec = util.find_spec(module)
        if spec is None or not spec.origin:
            raise ValueError(f"{distribution} has no importable {module} package")
        package = Path(spec.origin).resolve().parent
        try:
            identity = json.loads((package / "_urltrakb_source.json").read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"{distribution} is missing its packaged source identity") from exc
        expected = {
            "distribution": distribution,
            "module": module,
            "version": version,
            "commit": commit,
        }
        if not isinstance(identity, dict) or any(identity.get(k) != v for k, v in expected.items()):
            raise ValueError(f"{distribution} packaged source identity does not match the baseline")
        verified[distribution] = package
    return verified


def verify_local_vendors(root: Path) -> dict[str, Path]:
    """Reject registry packages and other editable checkouts before a source build."""
    verified = verify_installed_vendors()
    for folder, distribution, module, _, _ in VENDORS:
        expected = (root / "vendor" / folder / module).resolve(strict=True)
        if verified[distribution] != expected:
            raise ValueError(
                f"{distribution} must load from this checkout's vendor/{folder}. "
                "Run uv sync --frozen in the source directory before building."
            )
        verify_vendor_source(root / "vendor" / folder)
    return verified


if __name__ == "__main__":
    for distribution, package in verify_local_vendors(Path(__file__).resolve().parents[1]).items():
        print(f"{distribution}: {package}")
