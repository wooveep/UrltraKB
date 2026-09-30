"""Conservative ownership across current inputs, history and pending work."""

from dataclasses import dataclass
from pathlib import Path

from openkb.artifact_links import linked_artifacts
from openkb.file_state import contained_paths
from openkb.ingest_records import KnowledgeRevision, Proposal
from openkb.locks import kb_read_lock
from openkb.source_catalog import list_sources, read_record
from openkb.source_records import DiscoveryIntent, SourceRevision
from openkb.state import HashRegistry


@dataclass(frozen=True)
class ArtifactReferences:
    paths: frozenset[Path]
    complete: bool
    index_documents: frozenset[str] = frozenset()


def list_artifact_references(
    kb_dir: Path, *, excluding_registry_entry: str | None = None
) -> ArtifactReferences:
    """An unreadable owner prevents cleanup; absence is never inferred on error."""
    root = kb_dir.resolve()
    paths: set[Path] = set()
    indexes: set[str] = set()

    def retain(raw: str) -> None:
        if raw and "://" not in raw:
            paths.add((root / raw).resolve())

    def retain_tree(directory: Path) -> None:
        contained_paths(root, [directory])
        if directory.is_symlink():
            raise ValueError("Artifact owner is a symbolic link")
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise ValueError("Artifact owner contains a symbolic link")
            if path.is_file():
                paths.add(path.resolve())
                paths.update(linked_artifacts(root, path))

    with kb_read_lock(root / ".openkb"):
        try:
            for identity, entry in HashRegistry(root / ".openkb/hashes.json").all_entries().items():
                if identity == excluding_registry_entry:
                    continue
                if entry.get("doc_id"):
                    indexes.add(entry["doc_id"])
                for key in ("path", "raw_path", "source_path"):
                    retain(entry.get(key, ""))
            for source in list_sources(root):
                retain(source.identity)
            for path in (root / ".openkb/catalog/source-revisions").glob("*.json"):
                revision = read_record(root, "source-revisions", path.stem, SourceRevision)
                retain(revision.original)
                for asset in revision.assets:
                    if asset.artifact:
                        retain(asset.artifact)
            for path in (root / ".openkb/catalog/discovery-intents").glob("*.json"):
                retain(read_record(root, "discovery-intents", path.stem, DiscoveryIntent).original)
            from openkb.pending.records import ImportIntent

            for path in (root / ".openkb/catalog/import-intents").glob("*.json"):
                retain(read_record(root, "import-intents", path.stem, ImportIntent).payload)
            for path in (root / ".openkb/knowledge").glob("*/revisions/*/manifest.json"):
                contained_paths(root, [path])
                manifest = KnowledgeRevision.model_validate_json(path.read_text("utf-8"))
                for reference in manifest.original_references:
                    retain(reference)
                if manifest.index_ref:
                    indexes.add(manifest.index_ref)
                retain_tree(path.parent)
            for path in (root / ".openkb/proposals").glob("*/proposal.json"):
                contained_paths(root, [path])
                proposal = Proposal.model_validate_json(path.read_text("utf-8"))
                for reference in proposal.candidate_manifest.original_references:
                    retain(reference)
                if proposal.candidate_manifest.index_ref:
                    indexes.add(proposal.candidate_manifest.index_ref)
                retain_tree(path.parent)
            from openkb.knowledge_scope import live_scope
            from openkb.normalization import read_normalization
            from openkb.refresh_publication import read_refresh_candidate

            for path in (root / ".openkb/refresh-proposals").glob("*/proposal.json"):
                directory, refreshed = read_refresh_candidate(root, path.parent.name)
                for reference in refreshed.candidate_manifest.original_references:
                    retain(reference)
                retain_tree(directory)
            for path in (root / ".openkb/catalog/normalizations").glob("*.json"):
                directory, _ = read_normalization(root, path.stem)
                retain_tree(directory)
            retain_tree(root / "wiki")
            for path in (root / ".openkb/knowledge").glob("*/head.json"):
                retain_tree(live_scope(root, path.parent.name).wiki_dir)
        except (OSError, ValueError, TypeError):
            return ArtifactReferences(
                frozenset(paths), complete=False, index_documents=frozenset(indexes)
            )
    return ArtifactReferences(frozenset(paths), complete=True, index_documents=frozenset(indexes))
