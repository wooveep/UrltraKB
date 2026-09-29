"""Explicit remote-image policy and bounded acquisition during normalization only."""

import hashlib
import urllib.request
from http.client import HTTPException
from pathlib import Path
from urllib.parse import urlsplit

from openkb.config import resolve_effective_config
from openkb.locks import atomic_write_bytes
from openkb.markdown_images import ImageReference
from openkb.text_source import ResourcePolicy

REMOTE_POLICY = "http-images-v1:15s:20MiB"
_MAX_BYTES = 20 * 1024 * 1024
_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/avif": ".avif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
}


def resolve_resource_policy(kb_dir: Path, override: bool | None = None) -> ResourcePolicy:
    if override is not None:
        if type(override) is not bool:
            raise ValueError("download_remote_assets must be a boolean")
        return ResourcePolicy(download_remote_assets=override, source="single")
    config, sources = resolve_effective_config(kb_dir)
    return ResourcePolicy(
        download_remote_assets=config.get("download_remote_assets", False),
        source=sources.get("download_remote_assets", "default"),
    )


def retain_remote_images(
    references: list[ImageReference], wiki: Path, doc_name: str, policy: ResourcePolicy
):
    """Return exact source edits and per-reference errors; failures leave links intact."""
    edits: list[tuple[int, int, str]] = []
    failures: dict[str, str] = {}
    if not policy.download_remote_assets:
        return edits, failures
    retained: dict[str, str] = {}
    for reference in references:
        if reference.local_path is not None or reference.source.startswith("data:"):
            continue
        address = reference.source
        if address in failures:
            continue
        if address not in retained:
            try:
                if urlsplit(address).scheme not in {"http", "https"}:
                    raise ValueError("Only HTTP/HTTPS image resources can be downloaded")
                request = urllib.request.Request(
                    address, headers={"User-Agent": "OpenKB asset importer"}
                )
                with urllib.request.urlopen(request, timeout=15) as response:
                    mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    if mime not in _EXTENSIONS:
                        raise ValueError(
                            "Remote resource did not return a supported image media type"
                        )
                    body = response.read(_MAX_BYTES + 1)
                if not body or len(body) > _MAX_BYTES:
                    raise ValueError("Remote image is empty or exceeds the 20 MiB asset limit")
                name = hashlib.sha256(body).hexdigest() + _EXTENSIONS[mime]
                path = f"images/{doc_name}/remote/{name}"
                atomic_write_bytes(wiki / "sources" / path, body)
                retained[address] = path
            except (OSError, ValueError, HTTPException) as exc:
                failures[address] = f"{type(exc).__name__}: {exc}"
                continue
        edits.append((reference.start, reference.end, reference.relocated(retained[address])))
    return edits, failures
