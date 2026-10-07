"""Product spellings are evidence; only explicitly confirmed aliases identify a product."""

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from openkb.view_records import KnowledgeView, Product


def product_key(value: str) -> str:
    return re.sub(r"[\s\-‐‑‒–—]+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()


def confirmed_product(products: tuple[Product, ...], name: str) -> Product | None:
    aliases = [p for p in products if product_key(name) in {product_key(a) for a in p.aliases}]
    if len(aliases) > 1:
        raise ValueError("Ambiguous confirmed product alias; review product identities")
    if aliases:
        return aliases[0]
    exact = [p for p in products if p.name == name]
    if len(exact) > 1:
        raise ValueError("Duplicate product identity; confirm an alias before importing")
    return exact[0] if exact else None


@dataclass(frozen=True)
class ProductResolution:
    status: Literal["resolved", "ambiguous", "unresolved", "unscoped"]
    product_ids: frozenset[str] = frozenset()
    notes: tuple[str, ...] = ()


def _mentions(text: str, name: str):
    pattern = re.escape(name)
    if re.match(r"[a-z0-9_]", name):
        pattern = r"(?<![a-z0-9_])" + pattern
    if re.search(r"[a-z0-9_]$", name):
        pattern += r"(?![a-z0-9_])"
    return tuple(re.finditer(pattern, text))


def resolve_product_question(
    views: tuple[KnowledgeView, ...], question: str, products: tuple[Product, ...] = ()
) -> ProductResolution:
    text = product_key(question)
    names: dict[str, set[str]] = {}
    catalog = {p.product_id: p for p in products}
    for view in views:
        if view.product_id and view.product:
            product = catalog.get(view.product_id)
            labels = (view.product, *(product.aliases if product else ()))
            for label in labels:
                names.setdefault(product_key(label), set()).add(view.product_id)
    mentions = [(m.start(), m.end(), name) for name in names for m in _mentions(text, name)]
    mentions = [
        m
        for m in mentions
        if not any(other[0] <= m[0] and m[1] <= other[1] and other != m for other in mentions)
    ]
    selected: set[str] = set()
    ambiguous: set[str] = set()
    for _, _, name in mentions:
        candidates = set().union(
            *(ids for label, ids in names.items() if label == name or label.startswith(name + " "))
        )
        selected.update(candidates)
        if len(candidates) > 1:
            ambiguous.update(candidates)
    if ambiguous:
        details = tuple(
            f"Candidate product: {v.product}; product_id={v.product_id}; view={v.view_id}; "
            f"applicable versions={', '.join(v.applicable_versions) or 'unknown'}."
            for v in sorted(views, key=lambda v: (v.product or "", v.view_id))
            if v.product_id in ambiguous
        )
        return ProductResolution(
            "ambiguous",
            frozenset(selected),
            (
                "Ambiguous product names; no evidence scope was selected.",
                *details,
                "Select a view explicitly (--view / scope.view_id), or confirm product metadata "
                "through the source version review before querying across sources.",
            ),
        )
    # Known name fragments are hints, never an implicit authorization to search
    # every product. Ignore fragments already covered by a complete mention.
    fragments = {token for name in names for token in name.split() if len(token) >= 3}
    fragments.update(
        token
        for token in re.findall(r"[a-z][a-z0-9_.+]*", text)
        if len(token) >= 3 and any(token in name for name in names)
    )
    hints = {
        token
        for token in fragments
        for match in _mentions(text, token)
        if not any(start <= match.start() and match.end() <= end for start, end, _ in mentions)
    }
    if hints:
        return ProductResolution(
            "unresolved",
            notes=(
                "Unresolved product name: "
                + ", ".join(sorted(hints))
                + ". Confirm an alias or select a view (--view / scope.view_id); "
                "no evidence scope was selected.",
            ),
        )
    return ProductResolution("resolved" if selected else "unscoped", frozenset(selected))
