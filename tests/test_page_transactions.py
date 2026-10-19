"""Native page transactions preserve committed references across failed publication."""

import sqlite3

import pytest

from ultrakb.application.pages import delete_page, read_page, save_page
from ultrakb.application.views import published_scope
from ultrakb.desktop.verification_data import publish_pages
from ultrakb.storage.coordination import storage_stage


def _seed(root):
    publish_pages(
        root,
        {
            "concepts/a.md": b"---\ntype: Concept\n---\nOriginal",
            "concepts/b.md": b"See [[concepts/a]]",
            "index.md": b"- [[concepts/a]]\n- [[concepts/b]]",
        },
    )
    return published_scope(root)


@pytest.mark.parametrize("operation", ["edit", "delete"])
def test_page_failure_retains_body_index_backlinks_and_publication(kb_dir, operation):
    scope = _seed(kb_dir)
    before = {path: scope.knowledge.read(path) for path in scope.knowledge.files()}
    page = read_page(kb_dir, "concepts/a", scope=scope)
    with storage_stage(kb_dir, scope.knowledge.identity) as db, db.transaction() as sql:
        sql.execute(
            "CREATE TRIGGER fail_page BEFORE INSERT ON ukb_publications "
            "BEGIN SELECT RAISE(ABORT, 'Injected publication failure'); END"
        )
    with pytest.raises(sqlite3.Error, match="Injected publication failure"):
        if operation == "edit":
            save_page(
                kb_dir,
                "concepts/a",
                "Changed",
                version=page.version,
                publication_id=scope.publication_id,
                scope=scope,
            )
        else:
            delete_page(
                kb_dir,
                "concepts/a",
                version=page.version,
                publication_id=scope.publication_id,
                scope=scope,
            )
    current = published_scope(kb_dir)
    assert current.publication_id == scope.publication_id
    assert {path: current.knowledge.read(path) for path in current.knowledge.files()} == before
    assert read_page(kb_dir, "concepts/a").version == page.version


@pytest.mark.parametrize("operation", ["edit", "delete"])
def test_page_mutation_preserves_workcopy_symlink_and_reports_projection_conflict(
    kb_dir, operation
):
    scope = _seed(kb_dir)
    page = kb_dir / "wiki/concepts/a.md"
    target = kb_dir.parent / "unrelated-original.txt"
    target.write_bytes(b"Unrelated retained original")
    original = target.read_bytes()
    version = read_page(kb_dir, "concepts/a").version
    page.unlink()
    try:
        page.symlink_to(target)
    except OSError:
        pytest.skip("Creating symbolic links is unavailable")
    if operation == "edit":
        result = save_page(
            kb_dir,
            "concepts/a",
            "Changed",
            version=version,
            publication_id=scope.publication_id,
            scope=scope,
        )
        assert result.status == "saved"
        projection = result.projection
    else:
        result = delete_page(
            kb_dir, "concepts/a", version=version, publication_id=scope.publication_id, scope=scope
        )
        assert result["status"] == "deleted"
        projection = result["projection"]
    assert projection.status == "conflict"
    assert page.is_symlink() and target.read_bytes() == original
