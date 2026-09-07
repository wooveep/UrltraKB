"""Page mutations keep the page and its references consistent after failure."""

import pytest

from openkb.application.pages import read_page, save_page
from openkb.page_ops import delete_wiki_page


def test_page_delete_failure_restores_page_index_and_backlinks(kb_dir, monkeypatch):
    from openkb.agent import compiler

    page = kb_dir / "wiki/concepts/a.md"
    index = kb_dir / "wiki/index.md"
    link = kb_dir / "wiki/concepts/b.md"
    for path, text in ((page, "A"), (index, "- [[concepts/a]]"), (link, "See [[concepts/a]]")):
        path.write_text(text)
    before = {path: path.read_bytes() for path in (page, index, link)}
    original = compiler.remove_doc_from_index

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("index write failed")

    monkeypatch.setattr(compiler, "remove_doc_from_index", fail)
    with pytest.raises(OSError):
        delete_wiki_page(kb_dir, "concepts/a")
    assert {path: path.read_bytes() for path in before} == before
    assert not list((kb_dir / ".openkb/journal").glob("*.json"))


def test_page_save_failure_restores_body_and_metadata(kb_dir, monkeypatch):
    from openkb import page_ops

    page = kb_dir / "wiki/concepts/a.md"
    page.write_text("---\ntype: Concept\n---\nOriginal")
    version = read_page(kb_dir, "concepts/a").version
    original = page_ops.atomic_write_text

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("commit not reached")

    monkeypatch.setattr(page_ops, "atomic_write_text", fail)
    with pytest.raises(OSError):
        save_page(kb_dir, "concepts/a", "Changed", version=version)
    assert read_page(kb_dir, "concepts/a").version == version


@pytest.mark.parametrize("operation", ["edit", "delete"])
def test_page_mutation_preserves_symlink_and_target(kb_dir, operation):
    page = kb_dir / "wiki/concepts/a.md"
    target = kb_dir / "wiki/concepts/b.md"
    target.write_text("Retained original")
    try:
        page.symlink_to(target.name)
    except OSError:
        pytest.skip("Creating symbolic links is unavailable")
    with pytest.raises(ValueError, match="symbolic links"):
        if operation == "edit":
            save_page(kb_dir, "concepts/a", "Changed")
        else:
            delete_wiki_page(kb_dir, "concepts/a")
    assert page.is_symlink()
    assert target.read_text() == "Retained original"
