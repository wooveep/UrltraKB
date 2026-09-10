"""Freeze platform wheel URLs, hashes and sizes from the reviewed optional-runtime locks."""

import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests
import tomllib

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.tags import compatible_tags, cpython_tags
from packaging.utils import canonicalize_name, parse_wheel_filename

ROOT = Path(__file__).resolve().parents[1]


def catalog(profile, directory, requirements, platform):
    environment = {
        **default_environment(),
        "python_version": "3.12",
        "python_full_version": "3.12.13",
        "sys_platform": platform,
        "platform_system": "Windows" if platform == "win32" else "Linux",
        "os_name": "nt" if platform == "win32" else "posix",
        "platform_machine": "AMD64" if platform == "win32" else "x86_64",
    }
    platforms = (
        ["win_amd64"]
        if platform == "win32"
        else [
            *[f"manylinux_2_{minor}_x86_64" for minor in range(28, 16, -1)],
            "manylinux2014_x86_64",
            "manylinux2010_x86_64",
            "manylinux1_x86_64",
            "linux_x86_64",
        ]
    )
    tags = list(cpython_tags((3, 12), platforms=platforms)) + list(
        compatible_tags((3, 12), interpreter="cp312", platforms=platforms)
    )
    ranks = {tag: i for i, tag in enumerate(tags)}
    lock = tomllib.loads((directory / "uv.lock").read_text())
    selected = []
    for line in (directory / requirements).read_text().splitlines():
        line = line.strip()
        if not line.strip() or line.startswith(("#", "--")):
            continue
        r = Requirement(line)
        if r.marker and not r.marker.evaluate(environment):
            continue
        packages = [
            p
            for p in lock["package"]
            if p["name"] == canonicalize_name(r.name) and p["version"] in r.specifier
        ]
        if len(packages) != 1:
            raise ValueError(f"Ambiguous package: {r}")
        p = packages[0]
        candidates = []
        for wheel in p.get("wheels", []):
            name = unquote(Path(urlsplit(wheel["url"]).path).name)
            _, _, _, wtags = parse_wheel_filename(name)
            match = [ranks[t] for t in wtags if t in ranks]
            if match:
                candidates.append((min(match), name, wheel))
        if not candidates:
            raise ValueError(f"No {platform} wheel: {r}")
        _, name, w = min(candidates)
        size = w.get("size")
        if "hash" not in w:
            import hashlib

            sha = hashlib.sha256()
            size = 0
            with requests.get(w["url"], stream=True, timeout=(10, 45)) as response:
                response.raise_for_status()
                for chunk in response.iter_content(1024 * 1024):
                    size += len(chunk)
                    sha.update(chunk)
            w["hash"] = "sha256:" + sha.hexdigest()
        if size is None:
            with requests.get(w["url"], stream=True, timeout=30) as response:
                response.raise_for_status()
                size = int(response.headers["Content-Length"])
        selected.append(
            {
                "name": p["name"],
                "version": p["version"],
                "file": name,
                "url": w["url"],
                "sha256": w["hash"].removeprefix("sha256:"),
                "bytes": size,
            }
        )
    python = json.loads((ROOT / "packaging/ocr-runtime/interpreters.json").read_text())[platform]
    value = {
        "schema": 1,
        "profile": profile,
        "platform": platform,
        "python": {**python, "bytes": python["size"]},
        "wheels": selected,
        "model_manifest": "openvino-models.json" if profile == "openvino" else "native-models.json",
    }
    path = ROOT / "openkb/ocr/profiles" / f"{profile}-{platform}.json"
    path.write_text(json.dumps(value, indent=2) + "\n")
    print(profile, platform, len(selected), sum(v["bytes"] for v in selected))


if __name__ == "__main__":
    for profile, directory, req in [
        ("native", "ocr-runtime", "requirements.lock"),
        ("openvino", "ocr-openvino-runtime", "requirements-selected.txt"),
        ("nvidia", "ocr-nvidia-runtime", "requirements.lock"),
    ]:
        for platform in ("linux", "win32"):
            catalog(profile, ROOT / "packaging" / directory, req, platform)
