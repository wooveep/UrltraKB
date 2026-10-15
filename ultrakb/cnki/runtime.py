"""The converter uses the application's pinned PyMuPDF native runtime."""

import hashlib
import json
import sys
from pathlib import Path

CNKI_POLICY = "cnki-pdf-v1"
PYMUPDF_VERSION = "1.27.2.3"
MUPDF_VERSION = "1.27.2"
UPSTREAM_COMMIT = "6c4bc32b15ce748d211f45d536f5d5511ef9f368"


def processing_identity() -> dict:
    root = Path(__file__).parent
    identity: dict = {
        "policy": CNKI_POLICY,
        "upstream_commit": UPSTREAM_COMMIT,
        "required_pymupdf": PYMUPDF_VERSION,
        "required_mupdf": MUPDF_VERSION,
        "platform": sys.platform,
    }
    try:
        import pymupdf

        identity.update(
            pymupdf=pymupdf.version[0],
            mupdf=pymupdf.version[1],
            engine=hashlib.sha256((root / "engine.py").read_bytes()).hexdigest(),
            worker=hashlib.sha256((root / "worker.py").read_bytes()).hexdigest(),
        )
        native = Path(pymupdf.__file__).parent
        identity["native_files"] = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(native.iterdir())
            if path.is_file() and (path.suffix in {".so", ".dll", ".pyd"} or ".so." in path.name)
        }
        if pymupdf.version[:2] != (PYMUPDF_VERSION, MUPDF_VERSION) or not identity["native_files"]:
            raise ValueError(f"requires the bundled PyMuPDF {PYMUPDF_VERSION} native runtime")
    except (ImportError, OSError, ValueError) as exc:
        identity["unavailable"] = str(exc)
    return identity


def require_runtime(identity: dict) -> None:
    if identity.get("unavailable"):
        raise ValueError(f"CNKI runtime is unavailable: {identity['unavailable']}")


def identity_key(identity: dict) -> str:
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
