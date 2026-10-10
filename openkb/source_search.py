"""Pure matching over original units; native coordinates always refer to original text."""

import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass


def _key(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _tokens(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9_]{2,}|[\u3400-\u9fff]", _key(text)))


@dataclass(frozen=True)
class SourceUnit:
    location: dict
    text: str
    key: str
    tokens: frozenset[str]
    heading_tokens: frozenset[str]


def original_units(raw: list[dict] | dict | str) -> Iterator[tuple[dict, str]]:
    if isinstance(raw, list):
        for page in raw:
            yield {"pages": str(page["page"])}, page.get("content", "")
    elif isinstance(raw, dict) and raw.get("blocks"):
        for block in raw["blocks"]:
            yield (
                {"blocks": str(block["ordinal"])},
                "".join(raw["text"][a:b] for a, b in block["source_spans"]),
            )
    else:
        text = raw["text"] if isinstance(raw, dict) else raw
        # Whole paragraphs retain original offsets, including non-ASCII normalization.
        for match in re.finditer(r"[^\n]+(?:\n(?!\n)[^\n]+)*", text):
            yield {"chars": f"{match.start()}:{match.end()}"}, match[0]


class SourceSearch:
    """Build the original-unit token index once, then reuse it across proposals."""

    def __init__(self, raw: list[dict] | dict | str) -> None:
        self.raw = raw
        self.units = tuple(
            SourceUnit(
                location,
                text,
                _key(text),
                _tokens(text),
                _tokens(" ".join(re.findall(r"(?m)^#{1,6}\s+(.+)$", text))),
            )
            for location, text in original_units(raw)
        )
        self.by_token: dict[str, set[int]] = {}
        for ordinal, unit in enumerate(self.units):
            for token in unit.tokens:
                self.by_token.setdefault(token, set()).add(ordinal)


def search_units(
    index: SourceSearch, terms: list[str], *, literal_only: bool = False
) -> list[dict]:
    """Rank phrases, evidenced headings and topic coverage; never infer semantic completeness."""
    phrases = tuple(dict.fromkeys(_key(term).strip() for term in terms if term.strip()))
    tokens = _tokens(" ".join(phrases))
    candidates: set[int] = set()
    for token in tokens:
        candidates.update(index.by_token.get(token, ()))
    ranked = []
    for ordinal in candidates:
        unit = index.units[ordinal]
        matched = [
            term
            for term, key in zip(terms, map(_key, terms))
            if key.strip() and key.strip() in unit.key
        ]
        overlap = tokens & unit.tokens
        if not matched and (literal_only or not overlap):
            continue
        # A lone common Chinese character is not enough to locate an update.
        if not matched and not any(len(t) > 1 for t in overlap) and len(overlap) < 2:
            continue
        score = (len(matched), len(tokens & unit.heading_tokens), len(overlap), -len(unit.text))
        ranked.append(
            (
                score,
                ordinal,
                {"location": unit.location, "matched_terms": matched, "text": unit.text},
            )
        )
    ranked.sort(key=lambda row: (tuple(-n for n in row[0]), row[1]))
    return [hit for _, _, hit in ranked]
