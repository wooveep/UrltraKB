"""Standalone optional-runtime entry point; receives local paths, never Wiki permissions."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

EXPECTED = {"paddleocr": "3.7.0", "paddlex": "3.7.2", "paddlepaddle": "3.3.1"}


class RecognitionIncomplete(RuntimeError):
    pass


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as content:
        while block := content.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def run(plan: dict) -> None:
    if plan.get("supervised"):
        (Path(plan["output"]) / "worker.pid").write_text(str(os.getpid()), encoding="ascii")
        deadline = time.monotonic() + plan["resources"]["seconds"]
        while not (Path(plan["output"]) / "start").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("OCR ownership was not established")
            time.sleep(0.01)
    if sys.version_info[:3] != (3, 12, 13):
        raise ValueError("OCR runtime requires CPython 3.12.13")
    if any(importlib.metadata.version(name) != expected for name, expected in EXPECTED.items()):
        raise ValueError("OCR runtime package versions differ from the locked profile")
    root, source, output = (Path(plan[key]).resolve() for key in ("assets", "input", "output"))
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if digest(root / "manifest.json") != plan["assets_sha256"]:
        raise ValueError("OCR model package identity mismatch")
    for name, record in manifest["files"].items():
        file = root / name
        if (
            not file.resolve().is_relative_to(root)
            or file.is_symlink()
            or digest(file) != record["sha256"]
        ):
            raise ValueError("OCR model package contains a missing or changed asset")
    if digest(source) != plan["input_sha256"]:
        raise ValueError("OCR input version changed")
    for field in (
        "max_new_tokens",
        "min_pixels",
        "max_pixels",
        "threads",
        "max_regions",
        "max_tokens",
    ):
        if type(plan[field]) is not int or plan[field] <= 0:
            raise ValueError("OCR limits must be explicit positive integers")
    output.mkdir(parents=True, exist_ok=True)
    os.environ.update(
        {
            "PADDLE_PDX_CACHE_HOME": str(output / "cache"),
            "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
            "PADDLE_PDX_LOCAL_FONT_FILE_PATH": str(root / "fonts/SourceHanSansCN-Regular.otf"),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "OMP_NUM_THREADS": str(plan["threads"]),
            "OPENBLAS_NUM_THREADS": str(plan["threads"]),
        }
    )

    def offline(event, args):
        if event in {"socket.connect", "socket.getaddrinfo"}:
            raise RuntimeError("OCR offline runtime attempted network access")

    sys.addaudithook(offline)
    from paddleocr import PaddleOCRVL

    started = time.monotonic()
    loading_path = Path(__file__).with_name("loading.py")
    if digest(loading_path) != plan["loading_sha256"]:
        raise ValueError("OCR loading implementation changed after task admission")
    spec = importlib.util.spec_from_file_location("openkb_optional_ocr_loading", loading_path)
    if spec is None or spec.loader is None:
        raise ValueError("OCR loading implementation is unavailable")
    loading = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loading)
    with loading.native_cpu_loading():
        pipeline = PaddleOCRVL(
            pipeline_version="v1.6",
            device="cpu",
            cpu_threads=plan["threads"],
            layout_detection_model_dir=str(root / "PP-DocLayoutV3"),
            vl_rec_model_dir=str(root / "PaddleOCR-VL-1.6"),
            vl_rec_backend="native",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_layout_detection=True,
            use_queues=False,
            markdown_ignore_labels=[],
            use_ocr_for_image_block=True,
        )
    # This exact pinned PaddleX predictor receives the layout crops before
    # native VLM execution. Reserve every crop's output allowance up front.
    recognition = pipeline.paddlex_pipeline.vl_rec_model
    predict = recognition.predict
    usage = {"regions": 0, "reserved_output_tokens": 0, "calls": 0}
    model_config = json.loads((root / "PaddleOCR-VL-1.6/config.json").read_text())
    generation = json.loads((root / "PaddleOCR-VL-1.6/generation_config.json").read_text())
    context_tokens = model_config["max_position_embeddings"]
    eos, pad = generation["eos_token_id"], generation["pad_token_id"]
    if type(context_tokens) is not int or context_tokens <= 0 or (eos, pad) != (2, 0):
        raise ValueError("Unknown pinned recognition token contract")
    recognition.processor._DEFAULT_TEXT_KWARGS = {
        **recognition.processor._DEFAULT_TEXT_KWARGS,
        "truncation": False,
    }
    generate = recognition.infer.generate
    token_checks = []

    def save_usage():
        temporary = output / "usage.json.tmp"
        temporary.write_text(json.dumps(usage), encoding="utf-8")
        temporary.replace(output / "usage.json")

    save_usage()

    def bounded_generate(inputs, **options):
        shape = inputs["input_ids"].shape
        if len(shape) != 2 or shape[0] != 1 or shape[1] <= 0:
            raise RecognitionIncomplete("ocr_input_contract_unknown")
        if shape[1] + plan["max_new_tokens"] > context_tokens:
            raise RecognitionIncomplete("ocr_input_budget_exceeded")
        result = generate(inputs, **{**options, "max_new_tokens": plan["max_new_tokens"]})
        rows = result[0].tolist()
        if len(rows) != 1 or not isinstance(rows[0], list):
            raise RecognitionIncomplete("ocr_output_contract_unknown")
        ids = rows[0]
        if not ids or len(ids) > plan["max_new_tokens"] or eos not in ids:
            raise RecognitionIncomplete("ocr_output_incomplete")
        end = ids.index(eos)
        if any(token != pad for token in ids[end + 1 :]):
            raise RecognitionIncomplete("ocr_output_contract_unknown")
        token_checks.append({"input_tokens": shape[1], "output_tokens": end + 1, "eos": True})
        return result

    recognition.infer.generate = bounded_generate

    def bounded_predict(inputs, **options):
        if not isinstance(inputs, list):
            raise ValueError("Unknown local recognition input contract")
        regions = usage["regions"] + len(inputs)
        tokens = usage["reserved_output_tokens"] + len(inputs) * plan["max_new_tokens"]
        if regions > plan["max_regions"] or tokens > plan["max_tokens"]:
            raise RecognitionIncomplete("ocr_recognition_budget_exhausted")
        usage.update(regions=regions, reserved_output_tokens=tokens, calls=usage["calls"] + 1)
        save_usage()
        options["max_new_tokens"] = plan["max_new_tokens"]
        return predict(inputs, **options)

    recognition.predict = bounded_predict
    try:
        results = pipeline.predict(
            str(source),
            use_queues=False,
            max_new_tokens=plan["max_new_tokens"],
            min_pixels=plan["min_pixels"],
            max_pixels=plan["max_pixels"],
            markdown_ignore_labels=[],
            use_ocr_for_image_block=True,
        )
        if len(results) != 1:
            raise ValueError("A single local OCR image must produce exactly one result")
        result = results[0]
        payload = result.json["res"]
        payload.pop("input_path", None)
        markdown = result.markdown
        assets = {}
        for name, picture in markdown.get("markdown_images", {}).items():
            import io

            buffer = io.BytesIO()
            picture.save(buffer, format="PNG")
            content = buffer.getvalue()
            asset_id = hashlib.sha256(content).hexdigest()
            target = output / (asset_id + ".png")
            target.write_bytes(content)
            assets[name] = {"file": target.name, "sha256": asset_id, "bytes": len(content)}
        report = {
            "input_sha256": plan["input_sha256"],
            "assets_sha256": plan["assets_sha256"],
            "result": payload,
            "assets": assets,
            "markdown": markdown.get("markdown_texts", ""),
            "elapsed_seconds": time.monotonic() - started,
            "versions": EXPECTED,
            "usage": usage,
            "token_checks": token_checks,
        }
        temporary = output / "result.json.tmp"
        temporary.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        temporary.replace(output / "result.json")
    finally:
        pipeline.close()


if __name__ == "__main__":
    plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    try:
        run(plan)
    except RecognitionIncomplete as exc:
        target = Path(plan["output"]) / "failure.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps({"reason": str(exc)}), encoding="utf-8")
        temporary.replace(target)
        raise
