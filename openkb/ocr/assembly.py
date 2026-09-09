"""Local interpretation dependencies, separate from expensive OCR execution."""

from pathlib import Path

from openkb.state import HashRegistry

REVISION = "ocr-evidence-v1"


def assembly_profile(backend: str) -> dict:
    names = ["cloud_result.py"]
    if backend == "local":
        names.append("local_result.py")
    return {
        "revision": REVISION,
        "adapters": {
            name: HashRegistry.hash_file(Path(__file__).with_name(name)) for name in names
        },
    }
