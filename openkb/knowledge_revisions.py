"""Commit manual knowledge changes as immutable snapshots of a live view."""

import uuid
from contextlib import contextmanager
from typing import Iterator

from openkb.catalog_schema import CatalogSchema, catalog_schema_path
from openkb.ingest_records import KnowledgeHead, KnowledgeRevision, UnitRevision
from openkb.knowledge_scope import KnowledgeScope, live_scope
from openkb.mutation import mutation_scope
from openkb.source_catalog import read_record, read_source_revision, write_record
from openkb.unit_publication import copy_tree, read_head, wiki_versions


@contextmanager
def manual_knowledge_change(scope: KnowledgeScope) -> Iterator[None]:
    """The caller holds the write lease; generated baselines stay unchanged."""
    kb = scope.kb_dir
    if scope.wiki_dir != live_scope(kb, scope.view_id).wiki_dir:
        raise ValueError("Historical and staged knowledge cannot be edited directly")
    before = wiki_versions(kb, scope.wiki_dir)
    head = read_head(kb, scope.view_id)
    identity = uuid.uuid4().hex
    directory = kb / ".openkb/knowledge" / scope.view_id / "revisions" / identity
    head_path = kb / ".openkb/knowledge" / scope.view_id / "head.json"
    schema = catalog_schema_path(kb)
    with mutation_scope(
        kb, [scope.wiki_dir, directory, head_path, schema], operation="manual-knowledge"
    ):
        yield
        pages = wiki_versions(kb, scope.wiki_dir)
        if pages == before:
            return
        from openkb.knowledge_evidence import publication_dependencies

        # A manual edit does not verify a new source; preserve existing page provenance.
        dependencies = publication_dependencies(kb, head, pages, pages, head.inputs.values())
        originals = set()
        for revision_id in set(head.inputs.values()) | {
            identity for values in dependencies.values() for identity in values
        }:
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
            page_dependencies=dependencies,
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
                needs_refresh=head.needs_refresh,
            ),
        )
        write_record(schema, CatalogSchema())
