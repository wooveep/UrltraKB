"""Literal searches over validated original text, shared by retrieval and planning."""

from openkb.evidence import Evidence, complete_read_bound
from openkb.processing import processing_checkpoint


def literal_blocks(reader, source, parsed, query, *, max_chars=None):
    """Return all matching block positions; an incomplete scan is never a unique hit."""
    if not isinstance(query, str) or not query.strip() or len(query) > 512:
        raise ValueError("Invalid source search literal")
    if max_chars is not None and sum(block.chars for block in parsed.blocks) > max_chars:
        return None
    matches = []
    for index, block in enumerate(parsed.blocks):
        processing_checkpoint()
        reference = Evidence(source.source_id, source.id, parsed.id, block.id)
        view = reader.read(reference, max_chars=complete_read_bound(block))
        if len(view.text) != block.chars or view.next_start is not None:
            raise ValueError("Original source read was incomplete")
        if query.casefold() in view.text.casefold():
            matches.append(index)
    return matches
