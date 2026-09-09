"""Stream the pinned native PaddleOCR-VL weights into lazy CPU parameters.

PaddleX 3.7.2 derives checkpoint key names by splitting and concatenating
already allocated model tensors. Loading its complete FP32 model and checkpoint
at once exceeds small CPU hosts. This adapter implements the same pinned
transpose/fusion mapping one parameter at a time, with exact key/shape checks.
It is imported by the isolated optional worker, never installed into the app.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path


def load_native_weights(model_type, directory, *, dtype="float32", convert_from_hf=True, **options):
    if dtype != "float32" or convert_from_hf is not True or options:
        raise ValueError("Unsupported pinned CPU weight-loading options")
    import paddle
    from paddlex.inference.models.common.transformers.generation import GenerationConfig
    from paddlex.inference.models.common.transformers.transformers.model_utils import (
        dtype_guard,
        load_state_dict,
    )
    from safetensors import safe_open

    directory = Path(directory)
    checkpoint = directory / "model.safetensors"
    config = model_type.config_class.from_pretrained(str(directory))
    with paddle.LazyGuard(), dtype_guard(dtype):
        model = model_type(config)
    with safe_open(str(checkpoint), framework="paddle") as archive:
        available = set(archive.keys())
    transposed = {
        key
        for key in available
        if key.endswith("weight")
        and any(
            part in key
            for part in (
                "out_proj",
                "q_proj",
                "k_proj",
                "v_proj",
                "lm_head",
                "gate_proj",
                "up_proj",
                "down_proj",
                "o_proj",
                "linear_1",
                "linear_2",
                "fc",
                "in_proj",
            )
        )
    }
    used = set()
    for name, parameter in model.state_dict().items():
        split = None
        if name.startswith("model.") and "qkv_proj" in name:
            keys = [name.replace("qkv_proj", part) for part in ("q_proj", "k_proj", "v_proj")]
        elif name.startswith("model.") and "up_gate_proj" in name:
            keys = [name.replace("up_gate_proj", part) for part in ("gate_proj", "up_proj")]
        elif "head.attention." in name and any(
            f".{part}." in name for part in ("q_proj", "k_proj", "v_proj")
        ):
            part = next(part for part in ("q_proj", "k_proj", "v_proj") if f".{part}." in name)
            keys = [name.replace(f"{part}.", "in_proj_")]
            split = ("q_proj", "k_proj", "v_proj").index(part)
        else:
            keys = [name]
        if not set(keys) <= available:
            raise ValueError("Pinned OCR checkpoint is missing required parameters")
        weights = load_state_dict(
            str(checkpoint),
            fliter_dict_keys=set(keys),
            convert_from_hf=True,
            transpose_weight_keys=transposed,
        )
        if set(weights) != set(keys):
            raise ValueError("Pinned OCR selective weight loading returned unexpected keys")
        if split is not None:
            combined = weights[keys[0]]
            if combined.shape[-1] % 3:
                raise ValueError("Pinned OCR attention head cannot be split into Q/K/V")
            value = paddle.split(combined, 3, axis=-1)[split]
        elif len(keys) > 1:
            value = paddle.concat([weights[key] for key in keys], axis=-1)
        else:
            value = weights[keys[0]]
        value = value.astype(dtype)
        if list(value.shape) != list(parameter.shape):
            raise ValueError("Pinned OCR parameter shape mismatch")
        # Same ownership transfer as PaddleX's meta-model loader; no initialized
        # random parameter or full duplicate state dictionary is retained.
        with paddle.no_grad():
            parameter.get_tensor()._share_data_with(value.value().get_tensor())
        used.update(keys)
        del weights, value
    if used != available:
        raise ValueError("Pinned OCR checkpoint contains unconsumed parameters")
    if (directory / "generation_config.json").exists():
        model.generation_config = GenerationConfig.from_pretrained(str(directory))
    model.eval()
    return model


@contextmanager
def native_cpu_loading():
    from paddlex.inference.models.doc_vlm.modeling.paddleocr_vl._paddleocr_vl import (
        PaddleOCRVLForConditionalGeneration,
    )

    model_type = PaddleOCRVLForConditionalGeneration
    previous = model_type.__dict__.get("from_pretrained")
    model_type.from_pretrained = classmethod(load_native_weights)
    try:
        yield
    finally:
        if previous is None:
            del model_type.from_pretrained
        else:
            model_type.from_pretrained = previous
