"""Image-only import review exemptions cannot collide with literal broken URLs."""

import pytest

from ultrakb.storage.blobs import put_blob
from ultrakb.storage.coordination import storage_stage
from ultrakb.storage.database import create_database
from ultrakb.storage.import_review_assets import only_image_relocations

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("reverse", [False, True])
def test_readable_manual_image_and_unresolved_digest_looking_url_are_not_equivalent(
    tmp_path, reverse
):
    identity = create_database(tmp_path)
    page = "concepts/manual.md"
    with storage_stage(tmp_path, identity) as db:
        picture = put_blob(db, b"Retained image bytes")
        original = put_blob(db, b"Manual content\n\n![Original](../images/a.png)\n")
        broken = put_blob(db, f"Manual content\n\n![Original](sha256/{picture.digest})\n".encode())
        before = {page: original.digest, "images/a.png": picture.digest}
        after = {page: broken.digest, "images/a.png": picture.digest}
        if reverse:
            before, after = after, before
        assert not only_image_relocations(db, page, before, after)


def test_identical_bytes_under_a_reader_unsupported_path_still_require_review(tmp_path):
    from ultrakb.application.reading import read_knowledge_image
    from ultrakb.application.views import storage_scope
    from ultrakb.storage.knowledge_store import KnowledgeStore

    identity = create_database(tmp_path)
    page = "concepts/manual.md"
    files = {
        page: b"Manual content\n\n![Original](../images/a.png)\n",
        "images/a.png": b"Identical retained image bytes",
        "images/a.bin": b"Identical retained image bytes",
    }
    knowledge = KnowledgeStore(tmp_path, identity)
    knowledge.publish(knowledge.prepare_revision(files, expected=None))
    scope = storage_scope(tmp_path, identity)
    assert read_knowledge_image(tmp_path, "images/a.png", scope=scope)[1] == "image/png"
    with pytest.raises(ValueError, match="Unsupported knowledge image"):
        read_knowledge_image(tmp_path, "images/a.bin", scope=scope)
    with storage_stage(tmp_path, identity) as db:
        replacement = put_blob(db, b"Manual content\n\n![Original](../images/a.bin)\n")
        before = knowledge.bind().files()
        after = before | {page: replacement.digest}
        assert not only_image_relocations(db, page, before, after)
