"""Explicit OCR preparation/install use cases shared by desktop, REST and CLI."""

import json
import os
import shutil
import time
from pathlib import Path

import portalocker

from openkb import config
from openkb.locks import atomic_write_json, atomic_write_text
from openkb.ocr.install_files import acquire, digest, extract, run, verified
from openkb.ocr.installations import (
    installed_profiles,
    local_settings,
    package_plan,
    registry_path,
    runtime_defaults,
)
from openkb.processing import processing_checkpoint


def prepare_ocr_install(profile: str, destination: Path | None = None) -> dict:
    return package_plan(profile, destination)


def read_ocr_installations() -> list[dict]:
    result = []
    for row in installed_profiles():
        if row.get("state") == "ready":
            try:
                local_settings(row["id"])
            except (ValueError, OSError):
                row = {**row, "state": "not_ready", "reason": "ocr_runtime_not_ready"}
        result.append(row)
    return result


def _register(row):
    with config._with_global_config_lock():
        values = {r["id"]: r for r in installed_profiles()}
        values[row["id"]] = row
        atomic_write_json(registry_path(), values)


def install_ocr(
    profile: str,
    *,
    destination: Path | None = None,
    offline: Path | None = None,
    seconds: float = 3600.0,
    progress=None,
    cancelled=None,
) -> dict:
    import math

    if not math.isfinite(seconds) or not 0 < seconds <= 14400:
        raise ValueError("ocr_install_time_invalid")
    plan = prepare_ocr_install(profile, destination)
    root = Path(plan["destination"])
    identity = plan["id"]
    root.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    progress = progress or (lambda name, done, total: None)

    def check():
        processing_checkpoint()
        if cancelled and cancelled():
            raise InterruptedError("ocr_install_cancelled")
        if time.monotonic() - started >= seconds:
            raise TimeoutError("ocr_install_timeout")

    lock = portalocker.Lock(str(root / "install.lock"), timeout=0)
    while True:
        check()
        try:
            lock.acquire()
            break
        except portalocker.exceptions.LockException:
            time.sleep(0.1)
    row = {
        "id": identity,
        "profile": profile,
        "destination": str(root),
        "model": plan["model"],
        "state": "preparing",
        "reason": None,
    }
    try:
        try:
            existing = local_settings(identity)
            manifest = json.loads((Path(existing.assets) / "manifest.json").read_text())
            if Path(existing.assets).is_relative_to(root) and all(
                verified(Path(existing.assets) / name, record)
                for name, record in manifest["files"].items()
            ):
                return next(r for r in installed_profiles() if r["id"] == identity)
        except (OSError, ValueError):
            pass
        _register(row)
        staging = root / "cache" / identity
        staging.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(root).free < plan["download_bytes"] * 2:
            raise OSError("ocr_install_disk_space_insufficient")
        if offline:
            offline = offline.resolve()
            if json.loads((offline / "package.json").read_text()) != {
                "id": identity,
                "profile": profile,
                "platform": plan["runtime"]["platform"],
            }:
                raise ValueError("ocr_offline_package_identity_mismatch")
        for name, record in plan["files"].items():
            check()
            acquire(record, staging / name, check, progress, offline / name if offline else None)
        # Models are shared by content identity across CPU/GPU runtime packages.
        models_id = (
            __import__("hashlib")
            .sha256(json.dumps(plan["models"], sort_keys=True).encode())
            .hexdigest()
        )
        assets = root / "models" / models_id
        try:
            retained = json.loads((assets / "manifest.json").read_text())
            complete = all(
                verified(assets / name, record)
                for name, record in {**retained["files"], **plan["models"]["files"]}.items()
            )
        except (OSError, ValueError, KeyError, TypeError):
            complete = False
        if not complete:
            assets.mkdir(parents=True, exist_ok=True)
            for name in plan["models"]["files"]:
                check()
                target = assets / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(staging / "assets" / name, target)
            manifest = json.loads(json.dumps(plan["models"]))
            if profile == "openvino":
                extract(staging / "upstream.tar.gz", assets / "code", check, prefix=True)
                for path in sorted((assets / "code").rglob("*")):
                    if path.is_file():
                        manifest["files"][path.relative_to(assets).as_posix()] = {
                            "bytes": path.stat().st_size,
                            "sha256": digest(path),
                        }
            atomic_write_json(assets / "manifest.json", manifest)
        from openkb.ocr.installations import PROFILES

        for notice in ("NOTICE.txt", "LICENSE-Apache-2.0.txt"):
            atomic_write_text(assets / notice, (PROFILES / notice).read_text())
        runtime = root / "runtimes" / identity
        if runtime.exists():
            # Only our failed, unregistered package directory is replaceable.
            if (runtime / "ready.json").exists() and (
                runtime / ("python/python.exe" if os.name == "nt" else "python/bin/python3")
            ).is_file():
                # Leave an old registered runtime immutable. Restoring its shared
                # model files is sufficient; never delete a usable interpreter.
                restored = runtime_defaults(
                    runtime / ("python/python.exe" if os.name == "nt" else "python/bin/python3"),
                    assets,
                    "openvino" if profile == "openvino" else "native",
                )
                row.update(
                    state="ready",
                    receipt=str(runtime / "ready.json"),
                    settings=restored.model_dump(),
                )
                atomic_write_json(
                    runtime / "ready.json",
                    {
                        "id": identity,
                        "settings": restored.model_dump(),
                        "manifest_bytes": (assets / "manifest.json").stat().st_size,
                    },
                )
                _register(row)
                return row
            shutil.rmtree(runtime)
        runtime.mkdir(parents=True)
        extract(staging / "python.tar.gz", runtime, check)
        python = runtime / ("python/python.exe" if os.name == "nt" else "python/bin/python3")
        wheels = plan["runtime"]["wheels"]
        requirements = (
            "\n".join(f"{w['name']}=={w['version']} --hash=sha256:{w['sha256']}" for w in wheels)
            + "\n"
        )
        atomic_write_text(runtime / "requirements.txt", requirements)
        log = staging / "install.log"
        run(
            [
                str(python),
                "-I",
                "-m",
                "pip",
                "--isolated",
                "--disable-pip-version-check",
                "install",
                "--no-index",
                "--no-deps",
                "--require-hashes",
                "--only-binary=:all:",
                "--find-links",
                str(staging / "wheels"),
                "-r",
                str(runtime / "requirements.txt"),
            ],
            check,
            log,
        )
        run([str(python), "-I", "-m", "pip", "--isolated", "check"], check, log)
        settings = runtime_defaults(
            python, assets, "openvino" if profile == "openvino" else "native"
        )
        check()
        receipt = runtime / "ready.json"
        atomic_write_json(
            receipt,
            {
                "id": identity,
                "settings": settings.model_dump(),
                "manifest_bytes": (assets / "manifest.json").stat().st_size,
            },
        )
        row.update(state="ready", receipt=str(receipt), settings=settings.model_dump())
        _register(row)
        return row
    except BaseException as exc:
        row.update(
            state="cancelled" if isinstance(exc, InterruptedError) else "failed",
            reason=(
                str(exc)
                if str(exc).startswith("ocr_") and len(str(exc)) < 100
                else type(exc).__name__
            ),
        )
        _register(row)
        raise
    finally:
        lock.release()


def export_ocr_package(profile: str, directory: Path, *, seconds: float = 3600.0, progress=None):
    """Download the same complete pinned artifacts for a later network-free installation."""
    plan = prepare_ocr_install(profile)
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    def check():
        processing_checkpoint()
        if time.monotonic() - started >= seconds:
            raise TimeoutError("ocr_package_timeout")

    for name, record in plan["files"].items():
        acquire(record, directory / name, check, progress or (lambda *args: None))
    atomic_write_json(
        directory / "package.json",
        {"id": plan["id"], "profile": profile, "platform": plan["runtime"]["platform"]},
    )
    return {"directory": str(directory), "id": plan["id"], "download_bytes": plan["download_bytes"]}
