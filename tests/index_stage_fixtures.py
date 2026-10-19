"""Native index checkpoint ownership for standalone SDK algorithm fixtures."""

import pytest

from ultrakb.file_digest import hash_file
from ultrakb.storage.index_checkpoints import StoredIndexStages


@pytest.fixture
def index_stages(compile_work):
    def create(namespace, source, *, identity):
        work = compile_work(
            {
                "document": source.name,
                "source_digest": hash_file(source),
                "namespace": str(namespace),
            },
            {"index": identity},
        )
        return StoredIndexStages(work)

    return create
