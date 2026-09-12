"""Page text and required context through one bounded original-range cursor."""

from dataclasses import asdict

from openkb.evidence import Evidence, complete_read_bound


def original_window(reader, source, parsed, block, start, max_chars):
    total = block.chars + len(block.context)
    if start > total:
        raise ValueError("Original-source cursor exceeds text and context")
    reference = Evidence(source.source_id, source.id, parsed.id, block.id)
    view = reader.read(reference, max_chars=complete_read_bound(block))
    text_start = min(start, block.chars)
    text = view.text[text_start : text_start + max_chars]
    context_start = max(0, start - block.chars)
    context = view.context[context_start : context_start + max_chars - len(text)]
    following = start + len(text) + len(context)
    row = {
        **asdict(view),
        "text": text,
        "context": context,
        "context_start": context_start,
        "context_complete": following >= total,
        "context_only": start >= block.chars and bool(block.context),
        "next_start": following if following < total else None,
    }
    if text:
        row["reference"] = asdict(
            Evidence(
                source.source_id, source.id, parsed.id, block.id, text_start, text_start + len(text)
            )
        )
    return row
