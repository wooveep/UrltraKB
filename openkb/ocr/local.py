"""Run the separately installed CPU pipeline and validate immutable page results."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from openkb.compilation_report import report_auxiliary_warning
from openkb.evidence import BlockDraft, ParseStore
from openkb.inputs import processing_path
from openkb.locks import atomic_write_json, atomic_write_text
from openkb.ocr.assembly import assembly_profile
from openkb.ocr.cloud import CloudIncomplete
from openkb.ocr.config import LocalSettings
from openkb.ocr.local_result import parse_local_page
from openkb.ocr.local_usage import LocalUsage
from openkb.processing import ProcessingIncomplete, processing_checkpoint
from openkb.sources import SourceStore, SourceVersion, content_id, read_object
from openkb.state import HashRegistry


@contextmanager
def _workspace():
    directory = processing_path(prefix="openkb-ocr-")
    preserve = False
    try:
        yield directory
    except ProcessingIncomplete as exc:
        preserve = exc.reason == "ocr_cleanup_unconfirmed"
        raise
    finally:
        if not preserve:
            shutil.rmtree(directory)


class LocalOcr:
    def __init__(
        self,
        store: SourceStore,
        source: SourceVersion,
        config: LocalSettings,
        *,
        retries=None,
        device="cpu",
    ):
        self.store, self.source, self.config = store, source, config
        self.device = device
        self.fallback_reason = None
        self.started = time.monotonic()
        self.pages = 0
        self.usage = LocalUsage(store, source, config.limits)
        self.retries = retries or {}

    def close(self):
        pass  # Each optional worker is reaped before its page result returns.

    def page(self, document, page, *, input_id=None):
        from openkb.ocr.devices import device_slot

        if self.pages >= self.config.limits.max_pages:
            return [], "ocr_page_budget_exhausted"
        self.pages += 1
        candidates = ["cpu"] if self.device == "cpu" else ["gpu"]
        if self.device == "auto":
            candidates.append("cpu")
        for device in candidates:
            with device_slot(device, self.config.limits.seconds, self.started):
                blocks, reason = self._page(document, page, input_id=input_id, device=device)
            if (
                device == "gpu"
                and self.device == "auto"
                and reason
                in {
                    "ocr_gpu_unavailable",
                    "ocr_runtime_gpu_incompatible",
                    "ocr_device_initialization_failed",
                    "ocr_device_memory_exhausted",
                }
            ):
                self.fallback_reason = reason
                continue
            return blocks, reason
        return [], "ocr_runtime_not_ready"

    def _page(self, document, page, *, input_id=None, device="cpu"):
        profile = {
            "ocr": {**self.config.profile(), "device": device},
            "physical_page": page,
            "worker": self.config.profile()["worker"],
        }
        if input_id is not None:
            profile["embedded_input"] = input_id
        if page in self.retries:
            profile["reprocessing"] = self.retries[page]
        execution = {"input_key": self.source.input_key, "profile": dict(profile)}
        raw_path = self.store.owned_path(
            self.store.root / "local-ocr-results" / f"{content_id(execution)}.json"
        )
        profile["assembly"] = assembly_profile("local")
        parses = ParseStore(self.store.kb_dir)
        # A device ordinal alone cannot attest to current hardware or driver identity.
        reusable = device == "cpu"
        cached = parses.find(self.source, profile) if reusable else None
        if cached is not None:
            blocks = [
                BlockDraft(
                    self.store.asset(b.blob).read_text(encoding="utf-8"),
                    b.kind,
                    b.location,
                    b.assets,
                    b.context,
                )
                for b in cached.blocks
            ]
            reason = next(
                (row["reason"] for row in cached.quality if row["status"] == "needs_review"), None
            )
            return blocks, reason
        if reusable and raw_path.exists():
            record = read_object(raw_path)
            if record.get("input") != execution:
                return [], "ocr_result_identity_mismatch"
            return self._assemble(record, document, page, profile)
        if not Path(self.config.interpreter).is_file():
            return [], "ocr_runtime_not_installed"
        if not (Path(self.config.assets) / "manifest.json").is_file():
            return [], "ocr_model_package_missing"
        regions, tokens = self.usage.remaining
        if regions <= 0 or tokens < self.config.parameters.max_new_tokens:
            return [], "ocr_recognition_budget_exhausted"
        remaining = self.config.limits.seconds - (time.monotonic() - self.started)
        if remaining <= 0:
            return [], "ocr_time_budget_exhausted"
        with _workspace() as temporary:
            root = Path(temporary)
            input_path, output = root / "input.png", root / "output"
            rect = document[page - 1].rect
            scale = self.config.parameters.render_dpi / 72
            if rect.width * rect.height * scale**2 * 8 > self.config.limits.memory_bytes:
                return [], "ocr_render_memory_budget_exhausted"
            pixmap = document[page - 1].get_pixmap(dpi=self.config.parameters.render_dpi)
            # Private rendering has no publication rights and is covered by
            # the enclosing worker's task and source time limits.
            input_path.write_bytes(pixmap.tobytes("png"))
            output.mkdir()
            worker = Path(__file__).with_name(
                "openvino_worker.py" if self.config.runtime == "openvino" else "worker.py"
            )
            plan = {
                "device": device,
                "gpu_device": self.config.gpu_device,
                "physical_page": page,
                "input": str(input_path),
                "input_sha256": HashRegistry.hash_file(input_path),
                "output": str(output),
                "assets": self.config.assets,
                "assets_sha256": self.config.assets_sha256,
                "worker": str(worker),
                "supervised": True,
                "worker_sha256": profile["worker"],
                "loading_sha256": profile["ocr"]["loading"],
                **self.config.parameters.model_dump(exclude={"render_dpi"}),
                "max_regions": regions,
                "max_tokens": tokens,
                "resources": {**self.config.limits.model_dump(), "seconds": remaining},
            }
            plan_path = root / "plan.json"
            atomic_write_json(plan_path, plan)
            self.usage.begin(page, profile)
            try:
                status = self._execute(plan_path, output)
            except BaseException:
                try:
                    self.usage.finish(output)
                except Exception:
                    report_auxiliary_warning("ocr_usage_record_failed")
                raise  # Preserve cleanup ownership when execution is unconfirmed.
            else:
                self.usage.finish(output)
            if status["reason"]:
                return [], status["reason"]
            if status["exit_code"] != 0 or not (output / "result.json").exists():
                failure = output / "failure.json"
                if failure.is_file():
                    reason = read_object(failure).get("reason")
                    if reason in {
                        "ocr_gpu_unavailable",
                        "ocr_runtime_gpu_incompatible",
                        "ocr_device_initialization_failed",
                        "ocr_device_memory_exhausted",
                        "ocr_model_package_missing",
                        "ocr_time_budget_exhausted",
                        "ocr_input_contract_unknown",
                        "ocr_input_budget_exceeded",
                        "ocr_output_contract_unknown",
                        "ocr_output_incomplete",
                        "ocr_recognition_budget_exhausted",
                    }:
                        return [], reason
                return [], "ocr_runtime_failed"
            value = read_object(output / "result.json")
            if self.fallback_reason and isinstance(value.get("runtime"), dict):
                value["runtime"]["fallback_reason"] = self.fallback_reason
            if (
                value.get("input_sha256") != plan["input_sha256"]
                or value.get("assets_sha256") != plan["assets_sha256"]
            ):
                return [], "ocr_result_identity_mismatch"
            images = value.get("assets")
            if not isinstance(images, dict):
                return [], "ocr_result_assets_invalid"

            def asset(name):
                from openkb.ocr.cloud_result import verify_image

                record = images[name]
                if not isinstance(record, dict) or set(record) != {"file", "sha256", "bytes"}:
                    raise CloudIncomplete("ocr_result_assets_invalid")
                path = output / record["file"]
                if (
                    path.parent != output
                    or path.is_symlink()
                    or HashRegistry.hash_file(path) != record["sha256"]
                    or path.stat().st_size != record["bytes"]
                ):
                    raise CloudIncomplete("ocr_result_assets_invalid")
                content = path.read_bytes()
                verify_image(content)
                return content

            try:
                retained_assets = {name: self.store.put_bytes(asset(name)) for name in images}
            except CloudIncomplete as exc:
                return [], str(exc).replace("cloud_", "ocr_")
            record = {
                "input": execution,
                "source": self.source.id,
                "result_blob": self.store.put_bytes(json.dumps(value).encode("utf-8")),
                "assets": retained_assets,
                "dimensions": [pixmap.width, pixmap.height],
            }
            atomic_write_json(raw_path, record)
            return self._assemble(record, document, page, profile)

    def _assemble(self, record, document, page, profile):
        value = read_object(self.store.asset(record["result_blob"]))
        try:
            from openkb.ocr.openvino_result import parse_openvino_page

            adapter = parse_openvino_page if self.config.runtime == "openvino" else parse_local_page
            blocks, reason = adapter(
                value,
                document[page - 1],
                page,
                tuple(record["dimensions"]),
                self.store,
                lambda name: self.store.asset(record["assets"][name]).read_bytes(),
            )
        except CloudIncomplete as exc:
            return [], str(exc).replace("cloud_", "ocr_")
        if self.config.runtime == "native":
            from dataclasses import replace

            runtime = value.get(
                "runtime",
                {
                    "engine": "paddleocr",
                    "runtime": "native",
                    "model": "PaddleOCR-VL-1.6",
                    "devices": {"layout": "cpu", "language": "cpu"},
                },
            )
            blocks = [
                replace(b, context=json.dumps({"ocr": runtime, "detail": b.context}))
                for b in blocks
            ]
        quality = [
            {
                "page": page,
                "status": "needs_review" if reason else "verified",
                "reason": reason or "ocr_layout_checked",
            }
        ]
        ParseStore(self.store.kb_dir).save(self.source, profile, blocks, quality=quality)
        return blocks, reason

    def _execute(self, plan: Path, output: Path):
        process = subprocess.Popen(
            [
                self.config.interpreter,
                "-I",
                str(Path(__file__).with_name("supervisor.py")),
                str(plan),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            while process.poll() is None:
                processing_checkpoint("ocr")
                if time.monotonic() - self.started >= self.config.limits.seconds:
                    raise ProcessingIncomplete("ocr_time_budget_exhausted", "ocr")
                time.sleep(0.05)
        finally:
            if process.poll() is None:
                atomic_write_text(output / "stop", "stop")
                try:
                    process.wait(timeout=self.config.limits.cleanup_seconds)
                except subprocess.TimeoutExpired:
                    # Keep the optional descendants owned by the task's process
                    # tree. Its supervisor confirms their exit before cleanup.
                    raise ProcessingIncomplete("ocr_cleanup_unconfirmed", "ocr") from None
            process.wait()
        receipt = output / "supervision.json"
        if not receipt.is_file():
            raise ProcessingIncomplete("ocr_cleanup_unconfirmed", "ocr")
        result = read_object(receipt)
        if result.get("reaped") is not True:
            raise ProcessingIncomplete("ocr_cleanup_unconfirmed", "ocr")
        return result
