"""Standalone supervised OpenVINO 1.5 adapter for the pinned upstream implementation.

No application imports: this file executes in the optional inference environment.
The upstream batch/fallback path is replaced by serial per-block generation with
an EOS receipt. All original structured blocks and their visual crops are retained.
"""

import hashlib
import importlib.metadata
import json
import os
import platform
import sys
import time
from pathlib import Path

EXPECTED = {
    "openvino": "2025.4.1",
    "transformers": "4.54.0",
    "torch": "2.8.0+cpu",
    "torchvision": "0.23.0+cpu",
    "numpy": "2.2.6",
    "Pillow": "11.3.0",
}
COMMIT = "598857c1272c2b7224109ad4573974f7d4cc260f"


class Incomplete(RuntimeError):
    pass


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(output, name, value):
    temporary = output / (name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(output / name)


def device_failure(exc):
    text = str(exc).lower()
    if any(v in text for v in ("out of memory", "out_of_device_memory", "memory allocation")):
        return "ocr_device_memory_exhausted"
    if any(v in text for v in ("device lost", "device_lost", "driver", "cl_device_not_found")):
        return "ocr_device_initialization_failed"
    return None


def run(plan):
    output = Path(plan["output"])
    if plan.get("supervised"):
        (output / "worker.pid").write_text(str(os.getpid()), encoding="ascii")
        deadline = time.monotonic() + plan["resources"]["seconds"]
        while not (output / "start").exists():
            if time.monotonic() >= deadline:
                raise Incomplete("ocr_time_budget_exhausted")
            time.sleep(0.01)
    # Before any model execution, failed admission consumed no recognition tokens.
    initial_usage = {"regions": 0, "reserved_output_tokens": 0, "calls": 0}
    usage_path = Path(plan["output"]) / "usage.json"
    temporary_usage = usage_path.with_suffix(".tmp")
    temporary_usage.write_text(json.dumps(initial_usage), encoding="utf-8")
    temporary_usage.replace(usage_path)
    if sys.version_info[:3] != (3, 12, 13) or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise Incomplete("ocr_runtime_gpu_incompatible")
    if any(importlib.metadata.version(k) != v for k, v in EXPECTED.items()):
        raise Incomplete("ocr_runtime_gpu_incompatible")
    root, source = Path(plan["assets"]).resolve(), Path(plan["input"]).resolve()
    manifest = json.loads((root / "manifest.json").read_text())
    if (
        digest(root / "manifest.json") != plan["assets_sha256"]
        or manifest.get("code_commit") != COMMIT
    ):
        raise Incomplete("ocr_model_package_missing")
    canonical = json.loads((Path(__file__).parent / "profiles/openvino-models.json").read_text())
    if any(manifest["files"].get(k) != v for k, v in canonical["files"].items()):
        raise Incomplete("ocr_model_package_missing")
    for name, record in manifest["files"].items():
        path = root / name
        if (
            not path.is_file()
            or path.is_symlink()
            or not path.resolve().is_relative_to(root)
            or path.stat().st_size != record["bytes"]
            or digest(path) != record["sha256"]
        ):
            raise Incomplete("ocr_model_package_missing")
    code = root / "code"
    required = "paddleocr_vl_openvino/paddleocr_vl_pipeline/ov_paddleocr_vl_pipeline.py"
    if "code/" + required not in manifest["files"]:
        raise Incomplete("ocr_model_package_missing")
    if digest(source) != plan["input_sha256"]:
        raise Incomplete("ocr_input_contract_unknown")
    os.environ.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HOME": str(output / "cache"),
            "OMP_NUM_THREADS": str(plan["threads"]),
            "OPENBLAS_NUM_THREADS": str(plan["threads"]),
            "OV_CACHE_DIR": str(output / "cache"),
        }
    )
    os.chdir(output)

    def offline(event, args):
        if event in {"socket.connect", "socket.getaddrinfo"}:
            raise Incomplete("ocr_offline_network_attempt")

    sys.addaudithook(offline)
    sys.path.insert(0, str(code))
    import openvino as ov
    from paddleocr_vl_openvino.paddleocr_vl_pipeline.ov_paddleocr_vl_pipeline import PaddleOCRVL
    from PIL import Image

    core = ov.Core()
    device = "CPU"
    device_name = "CPU"
    driver = None
    if plan["device"] != "cpu":
        available = sorted(
            {
                "GPU.0" if v == "GPU" else v
                for v in core.available_devices
                if v == "GPU" or v.startswith("GPU.")
            }
        )
        requested = plan.get("gpu_device")
        if not available or (requested and requested not in available):
            raise Incomplete("ocr_gpu_unavailable")
        device = requested or available[0]
        device_name = str(core.get_property(device, "FULL_DEVICE_NAME"))
        try:
            driver = str(core.get_property(device, "DRIVER_VERSION"))
        except RuntimeError:
            pass  # This plugin/driver does not expose a driver version.
    usage = {"regions": 0, "reserved_output_tokens": 0, "calls": 0}
    save(output, "usage.json", usage)
    try:
        pipeline = PaddleOCRVL(
            layout_model_path=str(root / "PP-DoclayoutV3-ov/DocLayoutV3.xml"),
            vlm_model_path=str(root / "PaddleOCR-VL-1.5-ov"),
            layout_device="CPU",
            vlm_device=device,
            use_layout_detection=True,
            use_chart_recognition=True,
            use_seal_recognition=True,
            use_ocr_for_image_block=True,
            merge_layout_blocks=False,
            cache_dir=str(output / "cache"),
            llm_int4_compress=False,
            llm_int8_compress=False,
            llm_int8_quant=False,
            vision_int8_quant=False,
        )
    except RuntimeError as exc:
        if device != "CPU":
            raise Incomplete(device_failure(exc) or "ocr_device_initialization_failed") from None
        raise
    model = pipeline.vlm_model
    runtime = {
        "engine": "paddleocr",
        "runtime": "openvino",
        "model": "PaddleOCR-VL-1.5",
        "code_commit": COMMIT,
        "assets": plan["assets_sha256"],
        "precision": "upstream-fp16",
        "versions": EXPECTED,
        "device_name": device_name,
        "driver": driver,
        "devices": {
            "layout": "CPU",
            "vision": device,
            "embedding": device,
            "language": device,
            "projection": device,
        },
        "gpu_memory_limit": None,
    }
    # All five stages have been compiled by the full pipeline constructor.
    save(output, "capability.json", {"status": "loaded", "runtime": runtime})
    receipts = {}
    generate = model.generate
    current = {}

    def checked_generate(**kwargs):
        result = generate(**kwargs)
        ids = result[0].tolist()
        eos = model.tokenizer.eos_token_id
        current.update(
            tokens=len(ids),
            status="completed"
            if eos in ids
            else "length"
            if len(ids) >= plan["max_new_tokens"]
            else "unfinished",
        )
        return result

    model.generate = checked_generate

    def serial(block_imgs, text_prompts, block_infos=None, **kwargs):
        import cv2

        results = []
        if block_infos is None or not (len(block_imgs) == len(text_prompts) == len(block_infos)):
            raise Incomplete("ocr_input_contract_unknown")
        for image, prompt, info in zip(block_imgs, text_prompts, block_infos):
            receipt = {"status": "unfinished", "tokens": 0}
            key = json.dumps([info["label"], list(map(int, info["bbox"]))])
            if key in receipts:
                raise Incomplete("ocr_result_block_identity_invalid")
            receipts[key] = receipt
            if (
                usage["regions"] >= plan["max_regions"]
                or usage["reserved_output_tokens"] + plan["max_new_tokens"] > plan["max_tokens"]
            ):
                results.append({"result": ""})
                continue
            usage["regions"] += 1
            usage["calls"] += 1
            usage["reserved_output_tokens"] += plan["max_new_tokens"]
            save(output, "usage.json", usage)
            current.clear()
            try:
                image = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
                messages = [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image", "image": image},
                            {"type": "text", "text": prompt},
                        ],
                    }
                ]
                prepared = model.prepare_inputs(
                    messages,
                    image_processor_config={
                        "min_pixels": plan["min_pixels"],
                        "max_pixels": plan["max_pixels"],
                    },
                )
                input_tokens = int(prepared["inputs_embeds"].shape[1])
                context = int(model.config.max_position_embeddings)
                if input_tokens + plan["max_new_tokens"] > context:
                    raise Incomplete("ocr_input_budget_exceeded")
                text, stats = model.generate_from_prepared(
                    prepared,
                    {
                        "bos_token_id": model.tokenizer.bos_token_id,
                        "eos_token_id": model.tokenizer.eos_token_id,
                        "pad_token_id": model.tokenizer.pad_token_id,
                        "max_new_tokens": plan["max_new_tokens"],
                        "do_sample": False,
                    },
                    block_label=info["label"],
                )
                receipt.update(current, input_tokens=input_tokens)
                if receipt["status"] == "completed" and not text.strip():
                    receipt["status"] = "empty"
                results.append({"result": text})
            except Exception as exc:
                if device != "CPU" and (failure := device_failure(exc)):
                    raise Incomplete(failure) from None
                receipt.update(status="failed", reason="ocr_block_failed")
                results.append({"result": ""})
        return results

    pipeline._vlm_predict = serial
    try:
        values = list(
            pipeline.predict(
                str(source),
                max_new_tokens=plan["max_new_tokens"],
                vlm_batch_size=1,
                early_stop_ratio=0.0,
                use_chart_recognition=True,
                use_seal_recognition=True,
                use_ocr_for_image_block=True,
            )
        )
        if len(values) != 1:
            raise Incomplete("ocr_result_page_set_mismatch")
        value = values[0]
        image = Image.open(source)
        if (value["width"], value["height"]) != image.size:
            raise Incomplete("ocr_result_image_size_mismatch")
        blocks, assets = [], {}
        for order, block in enumerate(value["parsing_res_list"]):
            key = json.dumps([block.label, block.bbox])
            receipt = receipts.get(key, {"status": "unfinished", "tokens": 0})
            names = []
            if (
                block.label in {"image", "header_image", "footer_image", "seal", "chart"}
                or block.image
            ):
                # Retain a source crop even when upstream Markdown filters this label.
                name = f"page-{plan['physical_page']}-block-{order}.png"
                path = output / name
                image.crop(tuple(block.bbox)).save(path)
                assets[name] = {"file": name, "sha256": digest(path), "bytes": path.stat().st_size}
                names.append(name)
            # Upstream table tokens can refer to image objects; export them all.
            if block.image:
                for name, picture in [(block.image["path"], block.image["img"])]:
                    name = str(name)
                    filename = hashlib.sha256(name.encode()).hexdigest() + ".png"
                    path = output / filename
                    picture.save(path, format="PNG")
                    assets[name] = {
                        "file": filename,
                        "sha256": digest(path),
                        "bytes": path.stat().st_size,
                    }
                    if name not in names:
                        names.append(name)
            blocks.append(
                {
                    "id": order,
                    "order": order,
                    "label": block.label,
                    "bbox": block.bbox,
                    "text": block.content,
                    "assets": names,
                    **receipt,
                }
            )
        save(
            output,
            "result.json",
            {
                "input_sha256": plan["input_sha256"],
                "assets_sha256": plan["assets_sha256"],
                "runtime": runtime,
                "assets": assets,
                "result": {
                    "contract": "openkb-openvino-page-v1",
                    "physical_page": plan["physical_page"],
                    "width": image.width,
                    "height": image.height,
                    "blocks": blocks,
                },
                "usage": usage,
            },
        )
    finally:
        pipeline.close()


if __name__ == "__main__":
    plan = json.loads(Path(sys.argv[1]).read_text())
    try:
        run(plan)
    except Incomplete as exc:
        save(Path(plan["output"]), "failure.json", {"reason": str(exc)})
        raise
