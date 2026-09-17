"""Explicit adjacent figure references travel together without merging source identities."""

import re

from openkb.evidence import Evidence, complete_read_bound


def figure_pairs(reader, source, parsed):
    """Bind directional captions only inside one native paragraph/table scope."""
    pairs = {}
    texts = {}

    def text(block):
        if block.id not in texts:
            reference = Evidence(source.source_id, source.id, parsed.id, block.id)
            texts[block.id] = reader.read(reference, max_chars=complete_read_bound(block)).text
        return texts[block.id]

    def scope(block):
        return {key: value for key, value in block.location.items() if key != "paragraph"}

    blocks = parsed.blocks
    images = {
        block.id
        for block in blocks
        if block.location.get("kind") == "docx"
        and "paragraph" in block.location
        and (block.context_data or {}).get("image_relations")
        and text(block).lstrip().startswith("![")
    }
    index = 0
    while index < len(blocks):
        image = blocks[index]
        if image.id not in images:
            index += 1
            continue
        end = index + 1
        while end < len(blocks) and blocks[end].id in images and scope(blocks[end]) == scope(image):
            end += 1
        figures = list(blocks[index:end])
        for neighbor, pattern in (
            (
                index - 1,
                r"如下|下图|(?:following|below)\s+(?:figure|image)|(?:figure|image)\s+below",
            ),
            (end, r"上图|(?:previous|above)\s+(?:figure|image)|(?:figure|image)\s+above"),
        ):
            if not 0 <= neighbor < len(blocks):
                continue
            caption = blocks[neighbor]
            if (
                caption.kind not in {"paragraph", "table"}
                or caption.assets
                or caption.chars > 512
                or scope(caption) != scope(image)
                or not re.search(pattern, text(caption), re.I)
            ):
                continue
            group = [caption, *figures]
            for member in group:
                related = pairs.setdefault(member.id, [])
                related.extend(
                    other for other in group if other.id != member.id and other not in related
                )
        index = end
    return pairs
