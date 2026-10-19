"""Keep proven human additions outside a source refresh's generated editing scope."""

from difflib import SequenceMatcher

from ultrakb.agent.page_updates import PageUpdate


def manual_refresh_insertions(patch: PageUpdate, baseline: str) -> list[tuple[int, int]]:
    """Identify added lines, ignoring a preceding generated line's final newline."""
    generated = PageUpdate(baseline, source_ranges=patch.source_ranges).body.splitlines(
        keepends=True
    )
    current = patch.body.splitlines(keepends=True)
    offsets = [0]
    for line in current:
        offsets.append(offsets[-1] + len(line))
    comparison = SequenceMatcher(
        None,
        [line.rstrip("\r\n") for line in generated],
        [line.rstrip("\r\n") for line in current],
        autojunk=False,
    )
    return [
        (offsets[start], offsets[end])
        for kind, _, _, start, end in comparison.get_opcodes()
        if kind == "insert"
    ]


def manual_refresh_fragments(patch: PageUpdate, baseline: str) -> tuple[str, ...]:
    return tuple(
        text
        for start, end in manual_refresh_insertions(patch, baseline)
        if (text := patch.body[start:end].strip("\r\n")).strip()
    )


def editable_refresh_blocks(patch: PageUpdate, baseline: str) -> frozenset[str]:
    """Protect whole added blocks; mixed source paragraphs use literal preservation."""
    insertions = manual_refresh_insertions(patch, baseline)
    return frozenset(
        identity
        for identity, (start, end) in patch.blocks.items()
        if not any(left <= start and end <= right for left, right in insertions)
    )
