"""Check the optional runtime on CPU with Python network attempts denied.

This checks installed libraries, not model inference or OCR quality.
"""

import importlib.metadata
import json
import os
import sys


def deny_network(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname"}:
        raise RuntimeError("Offline runtime verification attempted network access")


sys.addaudithook(deny_network)
os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
assert sys.version_info[:3] == (3, 12, 13), "Python version mismatch"
for name, expected in {"paddleocr": "3.7.0", "paddlex": "3.7.2", "paddlepaddle": "3.3.1"}.items():
    if importlib.metadata.version(name) != expected:
        raise RuntimeError(f"Runtime version mismatch: {name}")
import paddle  # noqa: E402
from paddleocr import PaddleOCRVL  # noqa: E402, F401

paddle.set_device("cpu")
print(
    json.dumps(
        {"device": paddle.get_device(), "python": "3.12.13", "network": "python_audit_denied"}
    )
)
