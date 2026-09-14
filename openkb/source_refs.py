"""Inspect canonical source relationships without initializing an LLM runtime."""

import re
from pathlib import Path

from openkb import frontmatter


def has_unowned_metadata(text: str, *, summary: bool = False) -> bool:
    """Keep fields/comments that the page writer does not regenerate."""
    parts = frontmatter.split(text)
    if parts is None:
        return False
    block = parts[0]
    fields = (
        (
            "type",
            "description",
            "doc_type",
            "full_text",
            "source_id",
            "source_version",
            "parse_id",
            "title",
        )
        if summary
        else ("type", "description", "sources")
    )
    for field in fields:
        block = frontmatter.drop_line(block, field)
    return any(line.strip() and line != "---" for line in block.splitlines())


class SourceOwnership:
    """Separate source authorship from the latest accepted publication state."""

    def __init__(self, kb_dir: Path, source_id: str | None = None):
        from openkb.knowledge_commit import _baselines, _directory, _page_path, load_proposal
        from openkb.sources import SourceStore, content_id, read_object, valid_id

        self.wiki = kb_dir / "wiki"
        self.source_id = source_id
        self.baselines = _baselines(kb_dir)
        self.store = SourceStore(kb_dir)
        self.generated: dict[str, str | None] = {}
        if source_id is not None:
            receipt = _directory(kb_dir, "completed", f"{source_id}.json")
            if receipt.exists():
                record = read_object(receipt)
                # A different source may publish accepted link cleanup containing
                # manual edits. It cannot acquire authorship on this source's behalf.
                if "ownership" in record:
                    owned = read_object(_directory(kb_dir, "ownership", f"{source_id}.json"))
                    if (
                        content_id(owned) != record["ownership"]
                        or set(owned) != {"source_id", "proposal", "generated"}
                        or owned["source_id"] != source_id
                        or owned["proposal"] != record["proposal"]
                        or not isinstance(owned["generated"], dict)
                    ):
                        raise ValueError("Source ownership record does not match publication")
                    for name, digest in owned["generated"].items():
                        _page_path(kb_dir, name)
                        if digest is not None:
                            valid_id(digest)
                    self.generated = owned["generated"]
                else:
                    proposal = load_proposal(kb_dir, record["proposal"])
                    if proposal.source_id != source_id:
                        raise ValueError("Source ownership publication does not match")
                    self.generated = {**proposal.before, **proposal.changes}

    def requires_review(self, name: str, previous: str) -> bool:
        """A different source's accepted cleanup cannot authorize overwriting edits."""
        if self.source_id is None:
            return False
        text = self.store.asset(previous).read_text(encoding="utf-8")
        generated = self.generated.get(name)
        original = self.store.asset(generated).read_text(encoding="utf-8") if generated else ""
        if name.startswith("summaries/"):
            # Current metadata and markers are editable. The source's immutable
            # publication still identifies its summary if those fields are removed.
            if generated == previous:
                return False
            if generated is None:
                return True
            for value in (text, original):
                owner = frontmatter.parse(value).get("source_id")
                if (
                    not isinstance(owner, str)
                    or not owner
                    or owner == self.source_id
                    or f"<!-- openkb-source:{self.source_id} -->" in value
                ):
                    return True
            return False
        if not name.startswith(("concepts/", "entities/")):
            return False
        try:
            return source_contribution(text, self.source_id) != source_contribution(
                original, self.source_id
            )
        except ValueError:
            return True

    def can_delete(self, path: Path) -> bool:
        from openkb.state import HashRegistry

        name = path.relative_to(self.wiki).as_posix()
        digest = HashRegistry.hash_file(path)
        if self.baselines.get(name) != digest or self.generated.get(name) != digest:
            return False
        if self.source_id is None:
            return False
        text = path.read_text(encoding="utf-8")
        if has_unowned_metadata(text, summary=path.parent == self.wiki / "summaries"):
            return False
        parts = frontmatter.split(text)
        body = parts[1] if parts else text
        # A publication baseline can already contain accepted manual text.
        # Only this source's explicit range proves ownership of the body.
        remaining = withdraw_contribution(body, self.source_id)
        return remaining != body and not remaining.strip()

    def withdraw(self, path: Path, text: str, source_id: str) -> str:
        """A marker identifies ownership only while its generated bytes still agree."""
        name = path.relative_to(self.wiki).as_posix()
        digest = self.generated.get(name)
        if digest is None or name not in self.baselines:
            raise ValueError("Source contribution ownership has no known publication baseline")
        try:
            original = self.store.asset(digest).read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ValueError("Source contribution ownership baseline is unavailable") from exc
        remaining = withdraw_contribution(text, source_id)
        if source_contribution(text, source_id) != source_contribution(original, source_id):
            raise ValueError("Source contribution was edited; explicit review is required")
        return remaining


def scan_affected_pages(
    pages_dir: Path,
    source_file_marker: str,
    *,
    source_id: str | None = None,
    ownership: SourceOwnership | None = None,
) -> list[tuple[str, int]]:
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
        sources = fm_dict.get("sources")
        items = [str(x) for x in sources] if isinstance(sources, list) else []
        if source_file_marker in items:
            remaining = max(len(items) - 1, 0)
            if not remaining and ownership is not None and not ownership.can_delete(path):
                remaining = 1  # Preserve manual or unclassified content outside this source.
            affected.append((path.stem, remaining))
        elif source_id and (
            f"<!-- openkb-source:{source_id} -->" in text
            or f"<!-- /openkb-source:{source_id} -->" in text
        ):
            affected.append((path.stem, max(len(items), 1)))
    return affected


def withdraw_contribution(text: str, source_id: str) -> str:
    """Remove exactly one owned range, preserving all surrounding bytes."""
    opening, closing = f"<!-- openkb-source:{source_id} -->", f"<!-- /openkb-source:{source_id} -->"
    if opening not in text and closing not in text:
        return text
    if text.count(opening) != 1 or text.count(closing) != 1:
        raise ValueError("Ambiguous source contribution")
    start, end = text.index(opening), text.index(closing)
    interior = text[start + len(opening) : end]
    stack: list[str] = []
    for marker in re.finditer(r"<!-- (/?)openkb-source:([^>\s]+) -->", text[:start]):
        if marker[1]:
            if not stack or stack.pop() != marker[2]:
                raise ValueError("Ambiguous source contribution")
        else:
            if stack:
                raise ValueError("Ambiguous source contribution")
            stack.append(marker[2])
    if (
        end < start
        or stack
        or "<!-- openkb-source:" in interior
        or "<!-- /openkb-source:" in interior
    ):
        raise ValueError("Ambiguous source contribution")
    return text[:start] + text[end + len(closing) :]


def source_contribution(text: str, source_id: str) -> str | None:
    """Read the exact owned range after checking its complete marker structure."""
    remaining = withdraw_contribution(text, source_id)
    if remaining == text:
        return None
    start = text.index(f"<!-- openkb-source:{source_id} -->")
    return text[start : start + len(text) - len(remaining)]
