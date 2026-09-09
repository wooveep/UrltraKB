"""Optional-runtime contract probe: equal tiny HF checkpoint weights and logits.

Run under a resource supervisor with a JSON plan containing a fresh output path.
Uses the pinned runtime, no downloaded model and no network. This tests loading
compatibility, not real document recognition quality.
"""

import json
import os
import sys
from pathlib import Path


def main():
    plan = json.loads(Path(sys.argv[1]).read_text())
    root = Path(plan["output"])
    root.mkdir(exist_ok=True, parents=True)
    os.environ.update(
        PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="True",
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        OMP_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
    )

    def offline(event, args):
        if event in {"socket.connect", "socket.getaddrinfo"}:
            raise RuntimeError("Offline fixture")

    sys.addaudithook(offline)
    import numpy as np
    import paddle
    from paddlex.inference.models.doc_vlm.modeling.paddleocr_vl._config import PaddleOCRVLConfig
    from paddlex.inference.models.doc_vlm.modeling.paddleocr_vl._paddleocr_vl import (
        PaddleOCRVLForConditionalGeneration as Model,
    )
    from safetensors.numpy import save_file

    paddle.seed(13)
    config = PaddleOCRVLConfig(
        vocab_size=32,
        mm_hidden_size=16,
        hidden_size=16,
        intermediate_size=32,
        num_hidden_layers=1,
        num_attention_heads=2,
        num_key_value_heads=2,
        head_dim=8,
        max_position_embeddings=128,
        rope_scaling={"mrope_section": [1, 1, 2], "type": "default", "rope_type": "default"},
        vision_config={
            "hidden_size": 4,
            "intermediate_size": 8,
            "num_hidden_layers": 1,
            "num_attention_heads": 2,
            "image_size": 28,
            "patch_size": 14,
        },
    )
    original = Model(config)
    checkpoint = root / "checkpoint"
    checkpoint.mkdir()
    (checkpoint / "config.json").write_text(json.dumps(config.to_dict(saving_file=True)))
    transposed = original.get_transpose_weight_keys()
    arrays = {
        name: (value.numpy().T.copy() if name in transposed else value.numpy().copy())
        for name, value in original.get_hf_state_dict().items()
    }
    save_file(arrays, str(checkpoint / "model.safetensors"), metadata={"format": "pt"})
    normal = Model.from_pretrained(str(checkpoint), dtype="float32", convert_from_hf=True)
    assert all(
        np.array_equal(value.numpy(), normal.state_dict()[name].numpy())
        for name, value in original.state_dict().items()
    )
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "native_loading", Path(__file__).resolve().parents[1] / "openkb/ocr/loading.py"
    )
    loading = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loading)
    lazy = loading.load_native_weights(Model, str(checkpoint))
    assert set(normal.state_dict()) == set(lazy.state_dict())
    assert all(
        np.array_equal(value.numpy(), lazy.state_dict()[name].numpy())
        for name, value in normal.state_dict().items()
    )
    normal.eval()
    lazy.eval()
    with paddle.no_grad():
        ids = paddle.to_tensor([[3, 4, 5]], dtype="int64")
        a = normal(
            input_ids=ids, attention_mask=paddle.ones_like(ids), return_dict=True
        ).logits.numpy()
        b = lazy(
            input_ids=ids, attention_mask=paddle.ones_like(ids), return_dict=True
        ).logits.numpy()
        assert np.array_equal(a, b)
    (root / "result.json").write_text(
        json.dumps(
            {
                "weights_equal": True,
                "forward_equal": True,
                "tensors": len(normal.state_dict()),
                "parameters": sum(v.numel().item() for v in normal.state_dict().values()),
            }
        )
    )
    print("Tiny OCR model eager/lazy loading weights equal")


if __name__ == "__main__":
    main()
