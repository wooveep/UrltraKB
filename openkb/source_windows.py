"""Page text and required context through one bounded original-range cursor."""

from dataclasses import asdict

from openkb.evidence import Evidence, complete_read_bound
from openkb.source_context import model_context


def original_window(reader, source, parsed, block, start, max_chars):
    if type(start) is not int or start < 0 or type(max_chars) is not int or max_chars <= 0:
        raise ValueError("Original-source window requires a nonnegative cursor and positive bound")
    context_text, context_format = model_context(block)
    total = block.chars + len(context_text)
    if start > total:
        raise ValueError("Original-source cursor exceeds text and context")
    reference = Evidence(source.source_id, source.id, parsed.id, block.id)
    view = reader.read(reference, max_chars=complete_read_bound(block))
    text_start = min(start, block.chars)
    text = view.text[text_start : text_start + max_chars]
    context_start = max(0, start - block.chars)
    context = context_text[context_start : context_start + max_chars - len(text)]
    following = start + len(text) + len(context)
    row = {
        **{key: value for key, value in asdict(view).items() if key != "context_data"},
        "text": text,
        "context": context,
        "context_start": context_start,
        "context_format": context_format,
        "context_complete": following >= total,
        "context_only": start >= block.chars and bool(context_text),
        "next_start": following if following < total else None,
    }
    if text:
        row["reference"] = asdict(
            Evidence(
                source.source_id, source.id, parsed.id, block.id, text_start, text_start + len(text)
            )
        )
    return row
