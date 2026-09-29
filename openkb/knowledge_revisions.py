"""Commit manual knowledge changes as immutable snapshots of a live view."""

import uuid
from contextlib import contextmanager
from typing import Iterator

from openkb.catalog_schema import CatalogSchema, catalog_schema_path
from openkb.ingest_records import KnowledgeHead, KnowledgeRevision, UnitRevision
from openkb.knowledge_scope import KnowledgeScope, legacy_scope
from openkb.mutation import mutation_scope
from openkb.source_catalog import read_record, read_source_revision, write_record
from openkb.unit_publication import copy_tree, read_head, wiki_versions


@contextmanager
def manual_knowledge_change(scope: KnowledgeScope) -> Iterator[None]:
    """The caller holds the write lease; generated baselines stay unchanged."""
    kb = scope.kb_dir
    if scope.wiki_dir != legacy_scope(kb).wiki_dir:
        raise ValueError("Historical and staged knowledge cannot be edited directly")
    before = wiki_versions(kb, scope.wiki_dir)
    head = read_head(kb)
    identity = uuid.uuid4().hex
    directory = kb / ".openkb/knowledge/legacy/revisions" / identity
    head_path = kb / ".openkb/knowledge/legacy/head.json"
    schema = catalog_schema_path(kb)
    with mutation_scope(
        kb, [scope.wiki_dir, directory, head_path, schema], operation="manual-knowledge"
    ):
        yield
        pages = wiki_versions(kb, scope.wiki_dir)
        if pages == before:
            return
        originals = set()
        for revision_id in head.inputs.values():
            unit = read_record(kb, "unit-revisions", revision_id, UnitRevision)
            source = read_source_revision(kb, unit.source_revision_id)
            originals.add(source.original)
            originals.update(asset.artifact for asset in source.assets if asset.artifact)
        manifest = KnowledgeRevision(
            knowledge_revision_id=identity,
            view_id=scope.view_id,
            base_revision_id=head.knowledge_revision_id,
            change_kind="manual",
            input_revisions=tuple(sorted(head.inputs.values())),
            page_dependencies={
                name: tuple(sorted(head.inputs.values())) for name in pages if name.endswith(".md")
            },
            generated_baselines=head.generated_baselines,
            original_references=tuple(sorted(originals)),
        )
        copy_tree(scope.wiki_dir, directory / "wiki")
        write_record(directory / "manifest.json", manifest)
        write_record(
            head_path,
            KnowledgeHead(
                view_id=scope.view_id,
                generation=head.generation + 1,
                knowledge_revision_id=identity,
                inputs=head.inputs,
                generated_baselines=head.generated_baselines,
            ),
        )
        write_record(schema, CatalogSchema())
