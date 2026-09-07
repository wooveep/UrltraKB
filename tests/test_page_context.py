"""Committed reading context links back to the same KB and keeps missing sources visible."""


def test_page_context_includes_sources_outlinks_and_backlinks(kb_dir):
    from openkb.application.reading import read_page_context

    (kb_dir / "wiki/sources/paper.md").write_text("Original source")
    (kb_dir / "wiki/concepts/related.md").write_text("Related")
    (kb_dir / "wiki/concepts/note.md").write_text(
        "---\nsources: [paper, missing]\n---\n"
        "# Note\n[[concepts/related#section|Related]] [[concepts/absent]]"
    )
    (kb_dir / "wiki/index.md").write_text("[[concepts/note]]")
    (kb_dir / "wiki/concepts/inbound.md").write_text("[[concepts/note|Note]]")
    detail = read_page_context(kb_dir, "concepts/note")
    assert [ref.path for ref in detail.sources] == ["sources/paper", None]
    assert detail.sources[1].label == "missing"
    assert {(ref.path, ref.anchor) for ref in detail.outlinks} == {
        ("concepts/related", "section"),
        (None, ""),
    }
    assert {ref.path for ref in detail.backlinks} == {"index", "concepts/inbound"}
    assert detail.page.body.startswith("# Note")
    assert read_page_context(kb_dir, "index").outlinks[0].path == "concepts/note"


def test_reader_refuses_an_external_wiki_directory(kb_dir):
    import pytest

    from openkb.application.pages import read_page

    outside = kb_dir.with_name(kb_dir.name + "-outside")
    (kb_dir / "wiki").rename(outside)
    (outside / "index.md").write_text("External data")
    try:
        (kb_dir / "wiki").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks unavailable")
    with pytest.raises(ValueError, match="Invalid page path"):
        read_page(kb_dir, "index")


def test_unreadable_neighbor_keeps_current_page_and_marks_incomplete_links(kb_dir):
    from openkb.application.reading import read_page_context

    (kb_dir / "wiki/concepts/good.md").write_text("Readable content")
    (kb_dir / "wiki/concepts/bad.md").write_bytes(b"\xff")
    context = read_page_context(kb_dir, "concepts/good")
    assert context.page.body == "Readable content"
    assert len(context.problems) == 1
    assert "concepts/bad" in context.problems[0]
