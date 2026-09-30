"""Locate the application's complete private Office distribution, never system Office."""

import json
import platform
import sys
from pathlib import Path
from typing import Callable

from openkb.config import resolve_effective_config
from openkb.office.inventory import digest, inventory
from openkb.office.policy import LOAD_OPTIONS, OFFICE_POLICY, PDF_OPTIONS
from openkb.office.probe import probe
from openkb.office.records import OfficeManifest

OFFICE_VERSION = "26.2.6.3"
OFFICE_BUILD_ID = "8221e31b3ac356a1623c672912a3d2b492f7e3d1"


def runtime_path(kb_dir: Path) -> Path:
    config = resolve_effective_config(kb_dir)[0]
    explicit = config.get("office_runtime_path")
    if explicit:
        return Path(explicit)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "_internal/office"
    return Path(__file__).parent / "assets/runtime"


def processing_identity(kb_dir: Path) -> dict:
    """Plan even an unavailable Office attempt so its frozen input keeps a status."""
    root = runtime_path(kb_dir)
    path = root / "openkb-office.json"
    try:
        manifest_digest = digest(path)
    except OSError:
        manifest_digest = "unavailable"
    from openkb.office.fonts import font_supply

    try:
        fonts = font_supply()
    except (OSError, ValueError) as exc:
        fonts = {"unavailable": str(exc)}
    return {
        "policy": OFFICE_POLICY,
        "runtime_manifest": manifest_digest,
        "helpers": {
            name: digest(Path(__file__).with_name(name))
            for name in (
                "worker.py.txt",
                "supervisor.py.txt",
            )
        },
        "load_options": json.loads(json.dumps(LOAD_OPTIONS)),
        "pdf_options": json.loads(json.dumps(PDF_OPTIONS)),
        "font_supply": fonts,
        "locale": "C.UTF-8",
        "timezone": "UTC",
        "host": platform.platform(),
        "timeout": resolve_effective_config(kb_dir)[0].get("office_timeout_seconds") or 120,
    }


def require_runtime(
    kb_dir: Path,
    *,
    check_stop: Callable[[], None] = lambda: None,
    task_root: Path | None = None,
) -> tuple[Path, OfficeManifest, dict[str, str]]:
    root = runtime_path(kb_dir)
    manifest = validate_runtime(root)
    from openkb.office.fonts import font_supply

    font_supply()  # Fail this Office attempt if the current font supply cannot be recorded.
    actual = probe(
        root,
        manifest.python,
        manifest.soffice,
        manifest.launcher,
        check_stop=check_stop,
        timeout=min(20, resolve_effective_config(kb_dir)[0].get("office_timeout_seconds") or 120),
        task_root=task_root,
    )
    return root, manifest, actual


def validate_runtime(root: Path) -> OfficeManifest:
    """Verify an immutable distribution for both packaging and execution."""
    if not (root / "openkb-office.json").is_file():
        raise ValueError(
            f"Office runtime is unavailable: {root}; prepare the pinned complete distribution"
        )
    manifest = OfficeManifest.model_validate_json((root / "openkb-office.json").read_text())
    lock = json.loads(Path(__file__).with_name("runtime-lock.json").read_text())
    if manifest.platform != sys.platform:
        raise ValueError("Office runtime was prepared for a different platform")
    for name, actual in ((sys.platform, manifest.archive), ("source", manifest.source)):
        if actual.model_dump(exclude={"schema_version"}) != lock[name]:
            raise ValueError(
                "Office runtime provenance does not match the pinned official artifact"
            )
    if sys.platform == "win32" and manifest.launcher is None:
        raise ValueError("Office runtime is missing its owned-process Windows launcher")
    files, links = inventory(root)
    if files != manifest.files or links != manifest.links:
        raise ValueError("Office runtime file inventory changed or is incomplete")
    return manifest
