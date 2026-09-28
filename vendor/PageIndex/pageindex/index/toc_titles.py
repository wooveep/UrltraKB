"""Reconcile stale TOC titles against verifiable headings in the body."""

import json
import re

from .utils import extract_json


def _compact(text):
    return re.sub(r"\s+", "", text)


def _validated_heading(response, item, page_list, start_index, first, last):
    if not isinstance(response, dict) or response.get("same_section") is not True:
        return None
    page = response.get("physical_index")
    if type(page) is not int or not first <= page <= last:
        return None
    if not start_index <= page < start_index + len(page_list):
        return None
    heading, title = response.get("heading"), response.get("title")
    if not isinstance(heading, str) or not isinstance(title, str):
        return None
    heading, title = heading.strip(), title.strip()
    if not heading or not title or re.search(r"\.{2,}|…|⋯", heading):
        return None
    # Require a complete, literal heading line in the proposed body page.
    # Ignore PDF line wrapping/spacing, but not wording or heading boundaries.
    pattern = (
        r"(?m)^[ \t]*(?:#{1,6}[ \t]+)?"
        + r"\s*".join(re.escape(c) for c in _compact(heading))
        + r"[ \t]*$"
    )
    if not re.search(pattern, page_list[page - start_index][0]):
        return None
    numbered = re.match(r"^(\d+(?:\.\d+)*)(?:[.、)]?\s+)(.+)$", heading, re.S)
    section = str(item.get("structure") or "")
    if re.fullmatch(r"\d+(?:\.\d+)*", section):
        # A neighboring heading on the same page is not evidence for this entry.
        if not numbered or numbered.group(1) != section:
            return None
    actual_title = numbered.group(2).strip() if numbered else heading
    if _compact(actual_title) != _compact(title):
        return None
    return {
        "title": actual_title,
        "physical_index": page,
        "title_correction": {
            "toc_title": item["title"],
            "body_heading": heading,
            "physical_index": page,
        },
    }


async def reconcile_body_title(item, page_list, start_index, first, last, model, complete):
    """Return an evidenced replacement, or None when correspondence is unclear."""
    first = max(first, start_index)
    last = min(last, start_index + len(page_list) - 1)
    if first > last:
        return None
    pages = "\n".join(
        f"<physical_index_{page}>\n{page_list[page - start_index][0]}\n</physical_index_{page}>"
        for page in range(first, last + 1)
    )
    prompt = f"""The table of contents title could not be found in the document body.
The body is authoritative: the TOC may contain an outdated or erroneous title.
Find the actual heading for this SAME section, using its section number and
surrounding content. Do not substitute a neighboring section or an ordinary
sentence, and do not use a TOC listing as body evidence. If the correspondence
is uncertain, return {{"same_section": false}}.

TOC entry: {json.dumps(item, ensure_ascii=False)}

Return one JSON object with:
- "same_section": true only when the same section is identified
- "physical_index": the integer physical page where the heading appears
- "heading": the complete heading copied verbatim from the body, including its number
- "title": that exact heading with only the leading section number removed
Do not paraphrase or invent a heading. Preserve the body's wording.

Document body pages:
{pages}
"""
    response = await complete(model=model, prompt=prompt)
    return _validated_heading(extract_json(response), item, page_list, start_index, first, last)
