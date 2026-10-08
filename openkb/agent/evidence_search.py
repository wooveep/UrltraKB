"""Lexical navigation over pinned originals; search snippets are not proof."""

import json
from pathlib import Path

from openkb.application.query_views import QuerySelection, read_selected_file


def search_originals(
    selection: QuerySelection, terms: list[str], *, view_id: str = "", doc_name: str = ""
) -> dict:
    if not 1 <= len(terms) <= 6 or any(not t.strip() or len(t) > 80 for t in terms):
        raise ValueError("Use 1-6 short literal subject/configuration terms")
    needles = tuple(dict.fromkeys(t.casefold().strip() for t in terms))
    hits = []
    found_document = not doc_name
    for view in selection.views:
        if view_id and view.view_id != view_id:
            continue
        for path in sorted(view.files):
            if not path.startswith("sources/") or not path.endswith((".json", ".md")):
                continue
            name = (
                Path(path)
                .name.removesuffix(".content.json")
                .removesuffix(".json")
                .removesuffix(".md")
            )
            if doc_name and name != doc_name:
                continue
            found_document = True
            if path.endswith(".md") and any(
                f"sources/{name}{s}" in view.files for s in (".json", ".content.json")
            ):
                continue
            data = read_selected_file(view, path)
            raw = (
                json.loads(data) if path.endswith(".json") else data.decode("utf-8")
            )
            for locator, body in _units(raw):
                folded = body.casefold()
                found = [t for t in needles if t in folded]
                if not found:
                    continue
                first = min(folded.index(t) for t in found)
                hits.append(
                    {
                        "doc_name": name,
                        **locator,
                        "matched_terms": found,
                        "excerpt": body[max(0, first - 120) : first + 580],
                    }
                )
    if not found_document:
        raise ValueError(
            "Document not found in this snapshot. Read index.md for valid identifiers."
        )
    hits.sort(key=lambda hit: -len(hit["matched_terms"]))
    return {
        "status": "matches" if hits else "no_matches",
        "coverage": "search_hits_not_exhaustive",
        "hits": hits[:12],
        "total_hits": len(hits),
        "next_action": (
            "Read the cited original pages/blocks/cells. Search misses do not prove absence; "
            "page images may contain the answer."
        ),
    }


def _units(raw):
    if isinstance(raw, list):
        for page in raw:
            yield {"pages": str(page["page"])}, page.get("content", "")
    elif isinstance(raw, dict):
        if raw.get("blocks"):
            for block in raw["blocks"]:
                yield (
                    {"blocks": str(block["ordinal"])},
                    "".join(raw["text"][a:b] for a, b in block["source_spans"]),
                )
        else:
            yield {"chars": f"0:{len(raw['text'])}"}, raw["text"]
    else:
        yield {"path_read": True}, raw
