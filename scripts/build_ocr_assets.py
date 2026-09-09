"""Build a checked, revision-pinned optional OCR model package (explicit network step)."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import time
from pathlib import Path

import requests

MODELS = {
    "PP-DocLayoutV3": "7b48a7566925fa464281f930c58eee04fe2c862a",
    "PaddleOCR-VL-1.6": "c5630abae1d940eafe0697512a0325494b02ab42",
}


def build(output: Path, *, seconds: float, max_bytes: int) -> None:
    if not math.isfinite(seconds) or seconds <= 0 or max_bytes <= 0:
        raise ValueError("Asset builds require explicit positive time and byte bounds")
    started = time.monotonic()
    total = 0
    output.mkdir(parents=True, exist_ok=True)
    manifest = {"models": MODELS, "files": {}}
    with requests.Session() as session:

        def checkpoint():
            if time.monotonic() - started >= seconds:
                raise TimeoutError("OCR asset build time budget exhausted")

        for name, revision in MODELS.items():
            checkpoint()
            response = session.get(
                f"https://huggingface.co/api/models/PaddlePaddle/{name}/revision/{revision}",
                params={"blobs": "true"},
                timeout=(10, 30),
            )
            response.raise_for_status()
            metadata = response.json()
            if metadata.get("sha") != revision:
                raise ValueError("Model repository revision mismatch")
            for file in metadata["siblings"]:
                relative = Path(name) / file["rfilename"]
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("Model asset path escapes package")
                target = output / relative
                size = file.get("size")
                if type(size) is not int or size < 0:
                    raise ValueError("Model asset size is not verified")
                total += size
                if total > max_bytes:
                    raise ValueError("OCR asset package exceeds this build's byte allowance")
                url = f"https://huggingface.co/PaddlePaddle/{name}/resolve/{revision}/{file['rfilename']}"
                lfs = file.get("lfs", {})

                def hashes(path):
                    sha256 = hashlib.sha256()
                    git = hashlib.sha1(f"blob {size}\0".encode())
                    count = 0
                    with path.open("rb") as content:
                        while chunk := content.read(1024 * 1024):
                            checkpoint()
                            sha256.update(chunk)
                            git.update(chunk)
                            count += len(chunk)
                    if count != size or (
                        sha256.hexdigest() != lfs["sha256"]
                        if lfs
                        else git.hexdigest() != file["blobId"]
                    ):
                        raise ValueError("OCR asset digest mismatch")
                    return sha256.hexdigest()

                if target.exists():
                    digest = hashes(target)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_name(target.name + ".download")
                    try:
                        with session.get(url, stream=True, timeout=(10, 30)) as download:
                            download.raise_for_status()
                            received = 0
                            with temporary.open("wb") as destination:
                                for chunk in download.iter_content(128 * 1024):
                                    checkpoint()
                                    received += len(chunk)
                                    if received > size:
                                        raise ValueError("OCR asset exceeds declared size")
                                    destination.write(chunk)
                        digest = hashes(temporary)
                        temporary.replace(target)
                    finally:
                        temporary.unlink(missing_ok=True)
                manifest["files"][relative.as_posix()] = {
                    "sha256": digest,
                    "bytes": size,
                    "source": url,
                }
                print(relative.as_posix(), size, flush=True)
    fonts = Path(__file__).resolve().parents[1] / "assets/fonts"
    for name in ("SourceHanSansCN-Regular.otf", "SourceHanSansCN-OFL.txt"):
        target = output / "fonts" / name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(fonts / name, target)
        manifest["files"][f"fonts/{name}"] = {
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "bytes": target.stat().st_size,
            "source": "OpenKB/assets/fonts/" + name,
        }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seconds", required=True, type=float)
    parser.add_argument("--max-bytes", required=True, type=int)
    arguments = parser.parse_args()
    build(arguments.output, seconds=arguments.seconds, max_bytes=arguments.max_bytes)
