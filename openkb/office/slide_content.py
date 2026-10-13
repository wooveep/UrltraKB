"""The same explicitly labelled body/notes rule for every physical slide reader."""

from pathlib import Path

from openkb.office.records import OfficeConversion, Slide

LEGACY_NOTES_POLICY = "physical-slide-body-notes-v2"
NOTES_POLICY = "physical-slide-body-notes-v3"
NOTES_LABEL = "[演讲备注 / Speaker notes]"


def combine_parts(body: str, notes: str) -> str:
    return body + (f"\n\n{NOTES_LABEL}\n\n{notes}" if notes else "")


def attach_slides(pages: list[dict], slides: list[Slide], *, legacy: bool = False) -> list[dict]:
    if len(pages) != len(slides):
        raise ValueError("Slide map and extracted physical page counts disagree")
    result = []
    for ordinal, (page, slide) in enumerate(zip(pages, slides), 1):
        if page["page"] != ordinal or slide.page != ordinal or slide.ordinal != ordinal:
            raise ValueError("Slide map has inconsistent physical ordinals")
        if slide.body is None:
            raise ValueError("Slide has no frozen original body coordinates")
        from openkb.source_contract import NATIVE_PAGE_POLICY

        native = not legacy and page.get("source_structure_policy") == NATIVE_PAGE_POLICY
        body = page["content"] if native else slide.body
        parts = {"body": body, "notes": slide.notes}
        frozen_slide = slide.model_copy(update={"body": body}) if native else slide
        result.append(
            {
                **page,
                "content": combine_parts(page["content"], slide.notes),
                "parts": parts,
                "slide": frozen_slide.model_dump(mode="json"),
                "origin_locators": [
                    {"kind": "slide", "slide": ordinal, "part": part, "range": [0, len(text)]}
                    for part, text in parts.items()
                ],
            }
        )
    return result


def read_slides(provenance: Path) -> list[Slide]:
    return OfficeConversion.model_validate_json(provenance.read_text("utf-8")).slides


def require_slide_navigation(slides: list[Slide]) -> None:
    """Apply the same frozen body/notes boundary before full or segmented compilation."""
    if not slides:
        return
    from pageindex.index.page_parts_policy import PagePartsPolicy

    PagePartsPolicy(
        {
            "unit_count": len(slides),
            "page_parts": [{"body": slide.body, "notes": slide.notes} for slide in slides],
        }
    ).require_navigation_text()
