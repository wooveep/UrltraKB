"""Read-only acceptance of published scopes against frozen, independent expectations."""

import hashlib
import re
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ultrakb import frontmatter
from ultrakb.content_blocks import markdown_layout

_SECTION = re.compile(r"\*\*来源分节 (\d+) / (\d+)\*\*")
_POSITION = re.compile(
    r"(?:物理页|幻灯片) \d+(?:[–、-]\d+)*|字符区间 \[\d+, \d+\)(?:、\[\d+, \d+\))*"
)


class PublishedScopeExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    whole_scope: tuple[str, ...] = Field(min_length=1)
    section_scopes: tuple[tuple[str, ...], ...] = Field(min_length=2)
    literal_sha256: tuple[str, ...]
    link_targets: tuple[str, ...]

    @field_validator("path")
    @classmethod
    def relative_page(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "\\" in value or path.suffix != ".md":
            raise ValueError("Expected a relative wiki Markdown page")
        return value


def page_preservation_record(body: str) -> dict[str, tuple[str, ...]]:
    """Fingerprint quoted blocks and references, independent of scope rendering."""
    tokens, offsets = markdown_layout(body)
    spans = []
    end = -1
    for token in tokens:
        if token.map and token.type in {
            "fence",
            "code_block",
            "table_open",
            "blockquote_open",
            "html_block",
        }:
            start, stop = (offsets[line] for line in token.map)
            if start >= end:
                spans.append((start, stop))
                end = stop
    hashes = tuple(
        hashlib.sha256(body[start:stop].rstrip("\r\n").encode()).hexdigest()
        for start, stop in spans
    )
    links = ["wiki:" + link for link in re.findall(r"\[\[[^\]\n]+\]\]", body)]
    for token in tokens:
        for child in token.children or ():
            attribute = {"image": "src", "link_open": "href"}.get(child.type)
            if attribute:
                links.append(child.type + ":" + (child.attrGet(attribute) or ""))
            elif child.type == "html_inline":
                links.append("html:" + child.content)
    return {"literal_sha256": hashes, "link_targets": tuple(sorted(links))}


def _scopes(text: str) -> tuple[str, ...]:
    return tuple(
        position
        for line in text.splitlines()
        if line.startswith(("- 《", "本节来源："))
        for position in _POSITION.findall(line.split("》：", 1)[-1])
    )


def verify_published_scopes(
    wiki_dir: Path, expectations: Sequence[PublishedScopeExpectation]
) -> dict:
    """Check existing files; never generate, migrate, publish or spend model calls.

    Expected scopes and preservation hashes come from a frozen acceptance manifest,
    not by regenerating the expected page with the renderer under test.
    """
    if not expectations or len({e.path for e in expectations}) != len(expectations):
        raise ValueError("Acceptance requires a nonempty set of distinct page paths")
    wiki = wiki_dir.resolve()
    results = []
    for expected in expectations:
        path = (wiki / expected.path).resolve()
        if not path.is_relative_to(wiki):
            raise ValueError("Acceptance page escapes its wiki directory")
        issues = []
        if not path.is_file():
            results.append({"path": expected.path, "ok": False, "issues": ["missing_page"]})
            continue
        raw = path.read_bytes().decode("utf-8")
        parts = frontmatter.split(raw)
        body = parts[1] if parts else raw
        tokens, offsets = markdown_layout(body)
        markers = [
            (offsets[token.map[0]], match)
            for token in tokens
            if token.type == "inline" and token.level == 1 and token.map
            if (match := _SECTION.fullmatch(token.content))
        ]
        if not body.strip() or body.lstrip().splitlines()[0] != "## 本页来源范围":
            issues.append("missing_whole_scope")
        whole = body[: markers[0][0]] if markers else body
        if _scopes(whole) != expected.whole_scope:
            issues.append("wrong_whole_scope")
        if len(markers) != len(expected.section_scopes):
            issues.append("wrong_section_count")
        for index, (start, marker) in enumerate(markers):
            if (int(marker[1]), int(marker[2])) != (index + 1, len(expected.section_scopes)):
                issues.append(f"wrong_section_number:{index + 1}")
            stop = markers[index + 1][0] if index + 1 < len(markers) else len(body)
            if index < len(expected.section_scopes) and (
                _scopes(body[start:stop]) != expected.section_scopes[index]
            ):
                issues.append(f"wrong_section_scope:{index + 1}")
        record = page_preservation_record(body)
        if record["literal_sha256"] != expected.literal_sha256:
            issues.append("literal_blocks_changed")
        if record["link_targets"] != expected.link_targets:
            issues.append("references_changed")
        results.append({"path": expected.path, "ok": not issues, "issues": issues})
    return {"ok": all(page["ok"] for page in results), "pages": results}
