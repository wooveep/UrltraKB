"""Explicit full-pipeline checks on a built-in page, separate from package installation."""

import json
import tempfile
import time
from pathlib import Path

import pymupdf

from openkb import config
from openkb.locks import atomic_write_json
from openkb.ocr.cloud import CloudIncomplete
from openkb.ocr.devices import device_slot
from openkb.ocr.install_files import digest, run
from openkb.ocr.installations import local_settings
from openkb.ocr.local_result import parse_local_page
from openkb.ocr.openvino_result import parse_openvino_page
from openkb.processing import processing_checkpoint
from openkb.sources import SourceStore


def check_ocr_capability(
    installation: str, device: str = "auto", *, gpu_device: str | None = None, cancelled=None
) -> dict:
    from openkb.cancellation import cancellation_scope

    with cancellation_scope(cancelled or (lambda: False)):
        return _check_ocr_capability(installation, device, gpu_device)


def _check_ocr_capability(installation: str, device: str, gpu_device: str | None) -> dict:
    if device not in {"auto", "cpu", "gpu"}:
        raise ValueError("ocr_device_invalid")
    settings = local_settings(installation)
    if gpu_device is not None:
        from openkb.ocr.config import LocalSettings

        settings = LocalSettings.model_validate({**settings.model_dump(), "gpu_device": gpu_device})
    candidates = ["cpu"] if device == "cpu" else ["gpu", "cpu"] if device == "auto" else ["gpu"]
    failures: list[str] = []
    started = time.monotonic()

    def check():
        processing_checkpoint()
        if time.monotonic() - started > 180:
            raise TimeoutError("ocr_capability_timeout")

    for candidate in candidates:
        with tempfile.TemporaryDirectory(prefix="openkb-ocr-probe-") as directory:
            root = Path(directory)
            output = root / "output"
            output.mkdir()
            source = root / "sample.png"
            with pymupdf.open() as pdf:
                page = pdf.new_page(width=400, height=300)
                page.insert_text((30, 55), "OpenKB system test", fontsize=18)
                page.insert_text((30, 100), "The red switch controls the pump.", fontsize=13)
                page.get_pixmap(dpi=72).save(str(source))
            worker = (
                Path(__file__).parents[1]
                / "ocr"
                / ("openvino_worker.py" if settings.runtime == "openvino" else "worker.py")
            )
            plan = {
                "input": str(source),
                "input_sha256": digest(source),
                "output": str(output),
                "assets": settings.assets,
                "assets_sha256": settings.assets_sha256,
                "worker": str(worker),
                "worker_sha256": digest(worker),
                "supervised": True,
                "device": candidate,
                "gpu_device": settings.gpu_device,
                "physical_page": 1,
                **settings.parameters.model_dump(exclude={"render_dpi"}),
                "max_regions": 8,
                "max_tokens": 8192,
                "loading_sha256": digest(worker.with_name("loading.py")),
                "resources": {
                    **settings.limits.model_dump(),
                    "seconds": 180.0 - (time.monotonic() - started),
                },
            }
            atomic_write_json(root / "plan.json", plan)
            with device_slot(candidate, 180.0, started):
                run(
                    [
                        settings.interpreter,
                        "-I",
                        str(worker.with_name("supervisor.py")),
                        str(root / "plan.json"),
                    ],
                    check,
                    root / "supervisor.log",
                )
            receipt = json.loads((output / "supervision.json").read_text())
            if receipt.get("reaped") is not True:
                raise RuntimeError("ocr_cleanup_unconfirmed")
            if receipt.get("reason"):
                return {"status": receipt["reason"], "failures": failures}
            if (output / "result.json").exists() and receipt["exit_code"] == 0:
                value = json.loads((output / "result.json").read_text())
                parser = parse_openvino_page if settings.runtime == "openvino" else parse_local_page
                try:
                    with pymupdf.open() as sample:
                        page = sample.new_page(width=400, height=300)
                        blocks, reason = parser(
                            value,
                            page,
                            1,
                            (400, 300),
                            SourceStore(root),
                            lambda name: (output / value["assets"][name]).read_bytes(),
                        )
                    text = " ".join(block.text.lower() for block in blocks)
                    if reason or not all(word in text for word in ("openkb", "switch", "pump")):
                        return {
                            "status": "not_ready",
                            "failures": failures + [reason or "ocr_sample_mismatch"],
                        }
                except CloudIncomplete as exc:
                    return {"status": "not_ready", "failures": failures + [str(exc)]}
                result = {
                    "status": "ready",
                    "runtime": value.get("runtime"),
                    "failures": failures,
                    "sample": "openkb-built-in-v1",
                }
                with config._with_global_config_lock():
                    atomic_write_json(
                        config.GLOBAL_CONFIG_DIR
                        / "ocr-capabilities"
                        / f"{installation}-{device}.json",
                        result,
                    )
                return result
            failure = (
                json.loads((output / "failure.json").read_text())
                if (output / "failure.json").exists()
                else {}
            )
            reason = failure.get("reason", "ocr_runtime_failed")
            failures.append(reason)
            if reason not in {
                "ocr_gpu_unavailable",
                "ocr_runtime_gpu_incompatible",
                "ocr_device_initialization_failed",
                "ocr_device_memory_exhausted",
            }:
                break
    return {"status": "not_ready", "failures": failures}


def check_ocr_service(kb_dir: Path | None = None, *, cancelled=None) -> dict:
    """User-triggered sample check; never uses or saves a user's source document."""
    from openkb.cancellation import cancellation_scope
    from openkb.ocr.config import parsing_settings
    from openkb.ocr.service import PaddleService

    effective = (
        config.resolve_effective_config(kb_dir)[0] if kb_dir else config.load_global_config()
    )
    settings = parsing_settings(effective.get("parsing")).ocr.service
    if settings is None:
        return {"status": "not_ready", "reason": "ocr_service_not_configured"}
    connection = settings.profile()
    with cancellation_scope(cancelled or (lambda: False)):
        with tempfile.TemporaryDirectory(prefix="openkb-service-probe-") as directory:
            service = PaddleService(
                SourceStore(Path(directory)),
                None,
                settings,
                credential_root=kb_dir or config.GLOBAL_CONFIG_DIR,
            )
            try:
                with pymupdf.open() as document:
                    page = document.new_page(width=400, height=300)
                    page.insert_text((30, 55), "OpenKB system test", fontsize=18)
                    page.insert_text((30, 100), "The red switch controls the pump.", fontsize=13)
                    blocks, reason = service.page(document, 1)
            finally:
                service.close()
    text = " ".join(b.text.lower() for b in blocks)
    recognized = all(word in text for word in ("openkb", "switch", "pump"))
    return {
        "status": "connected_needs_review" if recognized else "not_ready",
        "connection": connection,
        "reason": reason or (None if recognized else "ocr_sample_mismatch"),
        "sample": "openkb-built-in-v1",
        "device": "service_managed_unreported",
    }
