"""Explicitly seal manually assembled storage inputs before HTTP reading tests."""


def publish_fixture(kb_dir):
    from ultrakb.locks import kb_ingest_lock
    from ultrakb.published_knowledge import initialize_publication

    with kb_ingest_lock(kb_dir / ".ultrakb"):
        return initialize_publication(kb_dir)


def page_preconditions(kb_dir, path):
    from ultrakb.application.pages import read_page
    from ultrakb.application.views import published_scope

    published = publish_fixture(kb_dir)
    try:
        page = read_page(kb_dir, path, scope=published_scope(kb_dir))
        version = page.version
    except (FileNotFoundError, ValueError):
        version = "missing-page"
    return {"publication_id": published.publication_id, "version": version}
