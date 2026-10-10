"""Lexical navigation over pinned originals; search snippets are not proof."""

import json
import re
from pathlib import Path

from openkb.application.query_views import QuerySelection, read_selected_file
from openkb.source_search import SourceSearch, search_units


def search_originals(
    selection: QuerySelection,
    terms: list[str],
    *,
    view_id: str = "",
    doc_name: str = "",
    indexes: dict | None = None,
) -> dict:
    if not 1 <= len(terms) <= 6 or any(not t.strip() or len(t) > 80 for t in terms):
        raise ValueError("Use 1-6 short literal subject/configuration terms")
    needles = tuple(dict.fromkeys(t.casefold().strip() for t in terms))
    indexes = {} if indexes is None else indexes
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
            from openkb.source_reading import native_source, reading_action

            source = native_source(view, path)
            identity = (view.view_id, path, view.files[path])
            if identity not in indexes:
                raw = json.loads(data) if path.endswith(".json") else data.decode("utf-8")
                indexes[identity] = SourceSearch(raw)
            index = indexes[identity]
            raw = index.raw
            for hit in search_units(index, list(needles), literal_only=True):
                locator, body, found = hit["location"], hit["text"], hit["matched_terms"]
                from openkb.original_context import original_context

                positions = sorted(
                    {
                        m.start()
                        for term in needles
                        for m in re.finditer(re.escape(term), body, re.IGNORECASE)
                    }
                )
                seen_scopes = set()
                for first in positions or [0]:
                    context = original_context(source, raw, locator, match_offset=first)
                    scope = json.dumps(
                        context["regions"][0]["headings"] if context["regions"] else []
                    )
                    if scope in seen_scopes:
                        continue
                    seen_scopes.add(scope)
                    hits.append(
                        {
                            "doc_name": name,
                            "unit_kind": source["unit_kind"],
                            "reading_action": reading_action(source, locator),
                            **locator,
                            "matched_terms": found,
                            "excerpt": context["regions"][0]["nearby_original"],
                            "source_context": context,
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
