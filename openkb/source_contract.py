"""Recorded source roles shared by native extraction and every source consumer.

Physical text has no Markdown syntax. Only extraction-recorded regions qualify
as tables or image occurrences; an asset inventory alone proves availability.
Legacy text and assets stay readable without inventing missing source roles.
"""

from dataclasses import asdict

from openkb.markdown_images import ImageReference

NATIVE_PAGE_POLICY = "native-page-roles-v1"
CELL_VALUE_POLICY = "visible-cell-values-v2"


def qualify_text(raw: dict, spans: list[list[int]]) -> dict:
    """Recorded format adapters qualify a full text source before selection."""
    from openkb.source_qualification import (
        source_headings,
        source_images,
        source_table_headers,
        source_tables,
    )

    return {
        "_qualified_headings": source_headings(raw),
        "_qualified_images": source_images(raw),
        "_qualified_tables": source_tables(raw, spans),
        "_qualified_table_headers": source_table_headers(raw),
    }


def page_roles(page: dict) -> list[dict]:
    policy = page.get("source_structure_policy")
    if policy is None:
        return []
    if policy != NATIVE_PAGE_POLICY:
        raise ValueError("Unsupported native source structure policy")
    roles = page.get("content_spans")
    if not isinstance(roles, list):
        raise ValueError("Native source roles require recorded content spans")
    stop = 0
    for role in roles:
        if not isinstance(role, dict) or role.get("role") not in {"table", "image"}:
            raise ValueError("Invalid native source role")
        span = role.get("span")
        if (
            not isinstance(span, list)
            or len(span) != 2
            or any(type(n) is not int for n in span)
            or not stop <= span[0] < span[1] <= len(page["content"])
        ):
            raise ValueError("Invalid native source role span")
        stop = span[1]
        generated = role.get("generated_span")
        if generated is not None and (
            role["role"] != "image"
            or not isinstance(generated, list)
            or len(generated) != 2
            or any(type(n) is not int for n in generated)
            or not 0 <= generated[0] <= span[0] < span[1] <= generated[1] <= len(page["content"])
        ):
            raise ValueError("Invalid native generated content span")
    return roles


def page_images(page: dict) -> list[ImageReference]:
    """Read recorded occurrences, including the previous generated-span format."""
    roles = page_roles(page)
    spans = [role["span"] for role in roles if role["role"] == "image"]
    if page.get("source_structure_policy") is None:
        previous_spans = page.get("image_reference_spans")
        if previous_spans is None:
            return []
        spans = previous_spans
    elif spans != page.get("image_reference_spans"):
        raise ValueError("Native image occurrence mappings disagree")
    images = page.get("images", [])
    if not isinstance(spans, list) or len(spans) != len(images):
        raise ValueError("Native image occurrences require one span per asset")
    result, stop = [], 0
    for span, image in zip(spans, images):
        if (
            not isinstance(span, list)
            or len(span) != 2
            or any(type(n) is not int for n in span)
            or not stop <= span[0] < span[1] <= len(page["content"])
        ):
            raise ValueError("Invalid native image occurrence span")
        left, right = span
        path = image.get("content_reference", image["path"])
        if page["content"][left:right] != path:
            raise ValueError("Native image occurrence differs from its recorded reference")
        # Captions require an explicit extraction record. Printed Markdown with
        # the same path cannot supply a caption for this native occurrence.
        result.append(ImageReference(left, right, image["path"], image.get("caption") or "", ""))
        stop = right
    return result


def qualify_pages(source: dict) -> dict:
    """Qualify full page bodies before any excerpt or body/notes projection."""
    from openkb.source_tables import native_table_layout

    tables: list[dict] = []
    images: list[dict] = []
    for left, _, ordinal in source["_page_segments"]:
        page = next(unit for unit in source["units"] if unit["page"] == ordinal)
        roles = page_roles(page)
        spans = [role["span"] for role in roles if role["role"] == "table"]
        tables.extend(native_table_layout(page["content"], spans, base=left))
        images.extend(
            {**asdict(ref), "start": left + ref.start, "end": left + ref.end}
            for ref in page_images(page)
        )
    return {**source, "_qualified_page_tables": tables, "_qualified_page_images": images}


def page_image_selection(page: dict, spans: list[dict], *, complete: bool) -> dict:
    """Asset availability and excerpt membership are distinct source facts."""
    references = page_images(page)
    inventory = page.get("images", [])
    ordinals = [
        ordinal
        for ordinal, _ in enumerate(inventory)
        if complete
        or (
            ordinal < len(references)
            and any(
                span["start"] <= references[ordinal].start < references[ordinal].end <= span["end"]
                for span in spans
            )
        )
    ]
    return {
        "images": [inventory[ordinal] for ordinal in ordinals],
        "image_ordinals": ordinals,
        "omitted_page_images": len(inventory) - len(ordinals),
        "image_position_coverage": "recorded"
        if len(references) == len(inventory)
        else "unconfirmed",
    }


def note_relative_pages(pages: list[dict]) -> list[dict]:
    """Render native references for source notes without rewriting literal paths."""
    from pageindex.backend.input_package import relocate_page

    return [
        relocate_page(
            page,
            [image["path"].removeprefix("sources/") for image in page.get("images", [])],
        )
        for page in pages
    ]
