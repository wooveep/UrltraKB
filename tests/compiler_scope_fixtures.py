"""Owned disposable files for direct compiler algorithm boundary tests."""

import pytest

from ultrakb.knowledge_scope import KnowledgeScope
from ultrakb.locks import PrivateMaterialization


@pytest.fixture
def private_compiler_scope(kb_dir):
    with PrivateMaterialization() as workspace:
        for section in ("sources", "summaries", "concepts", "entities", "explorations"):
            (workspace.directory / section).mkdir()
        yield KnowledgeScope(kb_dir, workspace.directory, private_workspace=workspace)
