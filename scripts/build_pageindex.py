"""Rebuild the project's pinned PageIndex wheel from verified upstream bytes.

Only the checked-in patch is applied. No installed environment is edited.
Run with the project's locked Python; Git is required to apply the patch.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import subprocess
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "packaging/pageindex"
UPSTREAM_VERSION = "0.3.0.dev3"
VERSION = "0.3.0.dev3+openkb.1"
UPSTREAM_HASH = "5056969108785f5c9c31e03ce60f5fa1043be96ecffb5a06353375212027b258"


def build() -> Path:
    upstream = ROOT / f"pageindex-{UPSTREAM_VERSION}-py3-none-any.whl"
    patch = ROOT / "openkb.patch"
    if hashlib.sha256(upstream.read_bytes()).hexdigest() != UPSTREAM_HASH:
        raise ValueError("PageIndex upstream wheel checksum differs from the reviewed baseline")
    output = ROOT / f"pageindex-{VERSION}-py3-none-any.whl"
    with tempfile.TemporaryDirectory(prefix="openkb-pageindex-build-") as temporary:
        source = Path(temporary)
        with zipfile.ZipFile(upstream) as archive:
            archive.extractall(source)
        apply = ["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf", "apply"]
        subprocess.run([*apply, "--check", str(patch)], cwd=source, check=True)
        subprocess.run([*apply, str(patch)], cwd=source, check=True)
        previous = source / f"pageindex-{UPSTREAM_VERSION}.dist-info"
        metadata = source / f"pageindex-{VERSION}.dist-info"
        previous.rename(metadata)
        path = metadata / "METADATA"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                f"Version: {UPSTREAM_VERSION}\n", f"Version: {VERSION}\n", 1
            ),
            encoding="utf-8",
            newline="\n",
        )
        (metadata / "RECORD").unlink()
        records = []
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as wheel:
            for file in sorted(
                source.rglob("*"), key=lambda path: path.relative_to(source).as_posix()
            ):
                if not file.is_file():
                    continue
                name = file.relative_to(source).as_posix()
                content = file.read_bytes()
                entry = zipfile.ZipInfo(name, date_time=(2026, 9, 9, 0, 0, 0))
                entry.create_system = 3
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = 0o644 << 16
                wheel.writestr(entry, content)
                digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
                records.append((name, "sha256=" + digest.decode(), len(content)))
            record = f"{metadata.name}/RECORD"
            records.append((record, "", ""))
            buffer = io.StringIO(newline="")
            csv.writer(buffer).writerows(records)
            entry = zipfile.ZipInfo(record, date_time=(2026, 9, 9, 0, 0, 0))
            entry.create_system = 3
            wheel.writestr(entry, buffer.getvalue())
    manifest = {
        "upstream_version": UPSTREAM_VERSION,
        "upstream_commit": "9ad54122bbd519cec8913198e2d63cff92781c1e",
        "upstream_sha256": UPSTREAM_HASH,
        "patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
        "version": VERSION,
        "wheel": output.name,
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "license": "MIT; upstream notices retained in the wheel",
    }
    (ROOT / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return output


if __name__ == "__main__":
    print(build())
