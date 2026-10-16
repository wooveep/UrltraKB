"""Locate supplied quotations without treating whitespace folding as semantic proof."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from ultrakb.agent.page_response import PageResponseError

_FURNITURE = re.compile(r"^(?:版权所有.*|copyright\b.*|第\s*\d+\s*页|\[Physical page \d+\])$", re.I)


def problem(rule: str, detail: str, *, path="$.content", line=1, column=1, **extra):
    field = {
        "path": path,
        "line": line,
        "column": column,
        "rule": rule,
        "expected": "a resolvable original reference",
        "actual": detail,
        **extra,
    }
    source = extra.get("source", {})
    kind = "candidate source" if extra.get("source_relation") == "closest_candidate" else "source"
    location = f"; {kind} window {source['window_span']}" if source.get("window_span") else ""
    change = extra.get("differences", [])
    if change:
        location += (
            f"; first difference {change[0]['source_codepoints']} -> "
            f"{change[0]['response_codepoints']}"
        )
    return PageResponseError(
        "page_excerpt_invalid",
        f"{path}:{line}:{column}: {detail}{location}",
        field_errors=(field,),
    )


@dataclass(frozen=True)
class ExcerptMatch:
    source: int
    start: int
    end: int
    mode: str


class ExcerptIndex:
    """One frozen window set owns matching, range checks and provenance."""

    def __init__(self, sources: list[str | dict], catalog: list[dict]):
        self.sources = sources
        self.texts = [s if isinstance(s, str) else s["content"] for s in sources]
        self.catalog = catalog
        # The retained v3 prompt deduplicates repeated paragraphs. Keep its bytes
        # unchanged, but give the reference protocol every actual occurrence.
        ranges = {(b["source"], b["start"], b["end"]): b for b in catalog}
        for block in catalog:
            text = self.texts[block["source"] - 1]
            body = text[block["start"] : block["end"]]
            start = text.find(body)
            while start >= 0:
                end = start + len(body)
                ranges.setdefault(
                    (block["source"], start, end), {**block, "start": start, "end": end}
                )
                start = text.find(body, end)
        self.ranges = list(ranges.values())

    def body(self, match: ExcerptMatch) -> str:
        return self.texts[match.source - 1][match.start : match.end].strip("\r\n")

    def locator(self, match: ExcerptMatch) -> dict:
        raw = self.sources[match.source - 1]
        text = self.texts[match.source - 1]
        pages = []
        if isinstance(raw, dict):
            before = list(re.finditer(r"\[Physical page (\d+)\]", text[: match.start]))
            within = re.findall(r"\[Physical page (\d+)\]", text[match.start : match.end])
            pages = ([int(before[-1][1])] if before else []) + list(map(int, within))
            if not pages:
                pages = [p["page"] for p in raw.get("page_spans", [])]
        contexts = [
            [block["start"], block["end"]]
            for block in self.ranges
            if block["source"] == match.source
            and block["start"] < match.end
            and block["end"] > match.start
        ]
        furniture: dict[tuple[int, int], dict] = {}
        if self._native(match.source):
            for start, end in contexts:
                cursor = start
                for line in text[start:end].splitlines(keepends=True):
                    if _FURNITURE.fullmatch(line.strip()):
                        furniture[cursor, cursor + len(line)] = {
                            "text": line.rstrip("\r\n"),
                            "window_span": [cursor, cursor + len(line)],
                        }
                    cursor += len(line)
        return {
            "source_number": match.source,
            "path": raw.get("path") if isinstance(raw, dict) else None,
            "window_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "window_span": [match.start, match.end],
            "selection_offset": raw.get("selection_offset") if isinstance(raw, dict) else None,
            "physical_pages": sorted(set(pages)),
            "source_sha256": raw.get("sha256") if isinstance(raw, dict) else None,
            "source_revision_id": raw.get("source_revision_id") if isinstance(raw, dict) else None,
            "context_spans": contexts,
            "printed_pages": [
                item
                for item in furniture.values()
                if re.fullmatch(r"第\s*\d+\s*页", item["text"].strip())
            ],
            "copyright": [
                item
                for item in furniture.values()
                if re.match(r"版权所有|copyright\b", item["text"].strip(), re.I)
            ],
        }

    def record(self, match: ExcerptMatch, candidate: str, **where) -> dict:
        return {
            "mode": match.mode,
            "source": self.locator(match),
            "source_sha256": hashlib.sha256(self.body(match).encode()).hexdigest(),
            **where,
        }

    def _native(self, source: int) -> bool:
        raw = self.sources[source - 1]
        return isinstance(raw, dict) and bool(raw.get("page_spans"))

    def references(self) -> dict[str, ExcerptMatch]:
        refs = {
            item["id"]: ExcerptMatch(item["source"], item["start"], item["end"], "reference")
            for item in self.catalog
        }
        covered = {(match.source, match.start, match.end) for match in refs.values()}
        for item in self.ranges:
            match = ExcerptMatch(item["source"], item["start"], item["end"], "reference")
            if (match.source, match.start, match.end) not in covered:
                text = self.texts[match.source - 1]
                key = hashlib.sha256(
                    f"{match.source}:{match.start}:{match.end}:{text}".encode()
                ).hexdigest()[:20]
                refs["r" + key] = match
            if not self._native(match.source):
                continue
            text = self.texts[match.source - 1]
            lines = text[match.start : match.end].splitlines(keepends=True)
            a, b = match.start, match.end
            while lines and (not lines[0].strip() or _FURNITURE.fullmatch(lines[0].strip())):
                a += len(lines.pop(0))
            while lines and (not lines[-1].strip() or _FURNITURE.fullmatch(lines[-1].strip())):
                b -= len(lines.pop())
            if a < b and (a, b) != (match.start, match.end):
                key = hashlib.sha256(f"{match.source}:{a}:{b}:{text}".encode()).hexdigest()[:20]
                refs["r" + key] = ExcerptMatch(match.source, a, b, "reference")
        return refs
