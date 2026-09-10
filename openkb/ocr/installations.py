"""Read-only runtime discovery and immutable package plans; never downloads at import."""

import json
import os
import platform
import sys
from pathlib import Path

from openkb import config
from openkb.ocr.config import LocalSettings
from openkb.ocr.install_files import digest, verified
from openkb.sources import content_id

PROFILES = Path(__file__).with_name("profiles")
NAMES = {
    "native": "PaddleOCR-VL-1.6 / CPU",
    "nvidia": "PaddleOCR-VL-1.6 / NVIDIA CUDA 12.6",
    "openvino": "PaddleOCR-VL-1.5 / Intel OpenVINO",
}


def default_root():
    if os.name == "nt":
        return (
            Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "OpenKB/ocr"
        )
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "openkb/ocr"


def package_plan(profile, destination=None):
    if (
        profile not in NAMES
        or sys.platform not in {"linux", "win32"}
        or platform.machine().lower() not in {"x86_64", "amd64"}
    ):
        raise ValueError("ocr_runtime_platform_unsupported")
    runtime = json.loads((PROFILES / f"{profile}-{sys.platform}.json").read_text())
    models = json.loads((PROFILES / runtime["model_manifest"]).read_text())
    files = {"python.tar.gz": runtime["python"]}
    files.update({f"wheels/{row['file']}": row for row in runtime["wheels"]})
    files.update({f"assets/{name}": row for name, row in models["files"].items()})
    if models.get("source"):
        files["upstream.tar.gz"] = models["source"]
    root = Path(destination or default_root()).expanduser().resolve()
    identity = content_id({"runtime": runtime, "models": models})
    return {
        "id": identity,
        "profile": profile,
        "label": NAMES[profile],
        "runtime": runtime,
        "models": models,
        "files": files,
        "download_bytes": sum(f["bytes"] for f in files.values() if not f.get("bundled")),
        "destination": str(root),
        "model": models.get("model", "PaddleOCR-VL-1.6"),
        "state": "available",
        "network": "Explicit installation downloads public packages only.",
    }


def registry_path():
    return config.GLOBAL_CONFIG_DIR / "ocr-installations.json"


def installed_profiles():
    path = registry_path()
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    if not isinstance(value, dict):
        raise ValueError("ocr_install_registry_invalid")
    result = []
    for identity, row in value.items():
        if (
            not isinstance(row, dict)
            or row.get("id") != identity
            or row.get("state") not in {"preparing", "ready", "failed", "cancelled"}
            or (row.get("state") == "ready" and not isinstance(row.get("receipt"), str))
        ):
            raise ValueError("ocr_install_registry_invalid")
        result.append(row)
    return result


def local_settings(identity):
    rows = [r for r in installed_profiles() if r["id"] == identity]
    if len(rows) != 1 or rows[0].get("state") != "ready":
        raise ValueError("ocr_runtime_not_installed")
    row = rows[0]
    receipt = Path(row["receipt"])
    if receipt.is_symlink() or not receipt.is_file():
        raise ValueError("ocr_runtime_not_ready")
    value = json.loads(receipt.read_text())
    if value.get("id") != identity:
        raise ValueError("ocr_runtime_not_ready")
    settings = LocalSettings.model_validate(value["settings"])
    if not Path(settings.interpreter).is_file() or not verified(
        Path(settings.assets) / "manifest.json",
        {"bytes": value["manifest_bytes"], "sha256": settings.assets_sha256},
    ):
        raise ValueError("ocr_runtime_not_ready")
    manifest = json.loads((Path(settings.assets) / "manifest.json").read_text())
    for name, record in manifest["files"].items():
        path = Path(settings.assets) / name
        if (
            not path.is_file()
            or path.is_symlink()
            or not path.resolve().is_relative_to(Path(settings.assets).resolve())
            or path.stat().st_size != record["bytes"]
        ):
            raise ValueError("ocr_model_package_missing")
    return settings


def runtime_defaults(interpreter, assets, runtime):
    return LocalSettings.model_validate(
        {
            "runtime": runtime,
            "interpreter": str(interpreter),
            "assets": str(assets),
            "assets_sha256": digest(assets / "manifest.json"),
            "limits": {
                "seconds": 600.0,
                "cleanup_seconds": 15.0,
                "max_pages": 100,
                "memory_bytes": 12 * 1024**3,
                "output_bytes": 64 * 1024**2,
                "max_regions": 2000,
                "max_tokens": 2_000_000,
            },
            "parameters": {
                "max_new_tokens": 2048,
                "min_pixels": 112896,
                "max_pixels": 1003520,
                "render_dpi": 120,
                "threads": 4,
            },
        }
    )
