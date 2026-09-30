"""One validated physical-page selection for source readers and evidence tools."""

from typing import Any


class PageRangeError(ValueError):
    """The requested range cannot be applied to this source."""


def select_page_part(source: dict, part: str | None) -> dict:
    """Keep physical ordinals while selecting one attributable slide domain."""
    if part is None:
        return source
    if part not in {"body", "notes"} or source.get("unit_kind") != "page":
        raise PageRangeError("Body/notes selection requires a physical slide source")
    units = []
    for page in source.get("units", []):
        parts = page.get("parts")
        if (
            not isinstance(parts, dict)
            or set(parts) != {"body", "notes"}
            or any(not isinstance(text, str) for text in parts.values())
        ):
            raise PageRangeError("This source has no verified body/notes partition")
        units.append(
            {
                **page,
                "content": parts[part],
                "part": part,
                "images": page.get("images", []) if part == "body" else [],
                "origin_locators": [
                    item for item in page.get("origin_locators", []) if item.get("part") == part
                ],
            }
        )
    label = "正文" if part == "body" else "演讲备注"
    return {
        **source,
        "part": part,
        "units": units,
        "origin_locators": [item for page in units for item in page["origin_locators"]],
        "content": "\n\n---\n\n".join(
            f"[Physical page {page['page']} · {label}]\n\n{page['content']}" for page in units
        ),
    }


def validate_source_ranges(pages=None, chars=None, blocks=None) -> None:
    if sum(value is not None for value in (pages, chars, blocks)) > 1:
        raise PageRangeError("Choose one of pages, characters, or blocks")


def read_page_selection(raw: Any, specification: str | None = None) -> dict:
    if not isinstance(raw, list):
        raise ValueError("Stored source must contain a page list")
    numbers = []
    for page in raw:
        if not isinstance(page, dict) or not isinstance(page.get("content"), str):
            raise ValueError("Stored source contains an invalid page body")
        number = page.get("page")
        if type(number) is not int or number < 1:
            raise ValueError("Stored source has invalid page ordinals")
        if not isinstance(page.get("images", []), list) or any(
            not isinstance(image, dict) or not isinstance(image.get("path"), str)
            for image in page.get("images", [])
        ):
            raise ValueError("Stored source has invalid image references")
        numbers.append(number)
    if numbers != sorted(set(numbers)):
        raise ValueError("Stored source ordinals must be unique and ordered")
    physical = bool(raw) and all(p.get("unit_kind") == "page" for p in raw)
    complete = physical and numbers == list(range(1, len(raw) + 1))
    if physical and not complete:
        raise ValueError("Physical page coverage is incomplete")
    if specification is not None:
        from pageindex.index.utils import parse_pages

        try:
            requested = parse_pages(specification)
        except (ValueError, TypeError) as exc:
            raise PageRangeError(str(exc)) from exc
        if not requested:
            raise PageRangeError("A page range must contain positive physical page numbers")
    else:
        requested = numbers
    selected = [page for page in raw if page["page"] in requested]
    missing = sorted(set(requested) - set(numbers))
    parts = []
    for page in selected:
        label = "Physical page" if physical else "Page"
        body = page["content"]
        block = f"[{label} {page['page']}]\n\n"
        if page.get("printed_page_label"):
            block += f"Printed label: {page['printed_page_label']}\n\n"
        block += body or "[No extracted text or images]"
        paths = ", ".join(image["path"] for image in page.get("images", []))
        if paths:
            block += f"\n[Images: {paths}]"
        parts.append(block)
    diagnostics = [] if complete else ["Physical-page coverage and extraction policy are unknown."]
    if missing:
        diagnostics.append(
            "No recorded content for requested ordinals: " + ", ".join(map(str, missing))
        )
    content = "\n\n---\n\n".join(parts)
    if diagnostics:
        content += "\n\n" + "\n".join(diagnostics)
    return {
        "content": content,
        "pages": len(raw) if complete else None,
        "unit_kind": "page" if physical else None,
        "page_range": [p["page"] for p in selected],
        "coverage": "complete" if complete else "unknown",
        "diagnostics": diagnostics,
        "units": selected,
        "origin_locators": [item for page in selected for item in page.get("origin_locators", [])],
    }
