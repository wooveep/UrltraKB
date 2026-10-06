"""Refuse to freeze a registry wheel or another checkout instead of vendored source."""

from importlib import metadata, util
from pathlib import Path


def verify_local_pageindex(root: Path) -> Path:
    expected = (root / "vendor/PageIndex/pageindex/__init__.py").resolve(strict=True)
    spec = util.find_spec("pageindex")
    if spec is None or not spec.origin or Path(spec.origin).resolve() != expected:
        raise ValueError(
            "PageIndex must load from this checkout's vendor/PageIndex. "
            "Run uv sync --frozen in the source directory before building."
        )
    if metadata.version("pageindex") != "0.3.0.dev3+urltrakb.6":
        raise ValueError("PageIndex metadata does not match the vendored source version")
    return expected.parent
