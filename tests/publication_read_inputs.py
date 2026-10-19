"""Explicitly publish manually assembled fixture pages before HTTP reading tests."""


def publish_fixture(kb_dir):
    from ultrakb.desktop.verification_data import publish_pages

    wiki = kb_dir / "wiki"
    return publish_pages(
        kb_dir,
        {
            path.relative_to(wiki).as_posix(): path.read_bytes()
            for path in wiki.rglob("*")
            if path.is_file()
        },
    )


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
