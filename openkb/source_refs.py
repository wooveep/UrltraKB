"""Inspect canonical source relationships without initializing an LLM runtime."""

from pathlib import Path

from openkb import frontmatter


def scan_affected_pages(pages_dir: Path, source_file_marker: str) -> list[tuple[str, int]]:
    """Return source-linked page names and remaining source counts.

    Preview and cleanup both use the canonical frontmatter parser. This scan
    stays importable without loading LLM SDKs into a native UI process.
    """
    affected: list[tuple[str, int]] = []
    if not pages_dir.is_dir():
        return affected
    for path in sorted(pages_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        fm_dict = frontmatter.parse(text)
        if not fm_dict:
            continue
        sources = fm_dict.get("sources")
        if not isinstance(sources, list):
            continue
        items = [str(x) for x in sources]
        if source_file_marker in items:
            affected.append((path.stem, max(len(items) - 1, 0)))
    return affected
