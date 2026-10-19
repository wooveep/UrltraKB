"""Authenticate retained inputs and exact outcomes of a source-selected refresh."""


def selected_pages(reasons, page_paths):
    """A page limit narrows pending work without weakening its publication evidence."""
    if page_paths is None:
        return set(reasons)
    pages = set(page_paths)
    if (
        not pages
        or len(pages) != len(page_paths)
        or pages - reasons.keys()
        or any(not p.startswith(("summaries/", "concepts/", "entities/")) for p in pages)
    ):
        raise ValueError("Select distinct pending generated page paths")
    return pages
