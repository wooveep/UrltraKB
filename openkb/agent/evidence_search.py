"""Lexical navigation over pinned originals; search snippets are not proof."""

import json
from pathlib import Path

from openkb.application.query_views import QuerySelection
from openkb.state import HashRegistry


def search_originals(
    selection: QuerySelection, terms: list[str], *, view_id: str = "", doc_name: str = ""
) -> dict:
    if not 1 <= len(terms) <= 6 or any(not t.strip() or len(t) > 80 for t in terms):
        raise ValueError("Use 1-6 short literal subject/configuration terms")
    needles = tuple(dict.fromkeys(t.casefold().strip() for t in terms))
    hits = []
    for view in selection.views:
        if view_id and view.view_id != view_id:
            continue
        for path, digest in sorted(view.files.items()):
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
            if path.endswith(".md") and any(
                f"sources/{name}{s}" in view.files for s in (".json", ".content.json")
            ):
                continue
            target = (view.scope.wiki_dir / path).resolve()
            if (
                not target.is_relative_to(view.scope.wiki_dir)
                or not target.is_file()
                or HashRegistry.hash_file(target) != digest
            ):
                raise ValueError("Original changed after selection; start a new question")
            raw = (
                json.loads(target.read_text("utf-8"))
                if path.endswith(".json")
                else target.read_text("utf-8")
            )
            for locator, body in _units(raw):
                folded = body.casefold()
                found = [t for t in needles if t in folded]
                if not found:
                    continue
                first = min(folded.index(t) for t in found)
                hits.append(
                    {
                        "view_id": view.view_id,
                        "doc_name": name,
                        **locator,
                        "matched_terms": found,
                        "excerpt": body[max(0, first - 120) : first + 580],
                    }
                )
    hits.sort(key=lambda hit: -len(hit["matched_terms"]))
    return {
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
