"""Conservative ownership across current inputs, history and pending work."""

from dataclasses import dataclass
from pathlib import Path

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


def list_artifact_references(kb_dir: Path) -> ArtifactReferences:
    """An unreadable owner prevents cleanup; absence is never inferred on error."""
    root = kb_dir.resolve()
    paths: set[Path] = set()

    def retain(raw: str) -> None:
        if raw and "://" not in raw:
            paths.add((root / raw).resolve())

    with kb_read_lock(root / ".openkb"):
        try:
            for entry in HashRegistry(root / ".openkb/hashes.json").all_entries().values():
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
            for path in (root / ".openkb/knowledge").glob("*/revisions/*/manifest.json"):
                contained_paths(root, [path])
                manifest = KnowledgeRevision.model_validate_json(path.read_text("utf-8"))
                for reference in manifest.original_references:
                    retain(reference)
            for path in (root / ".openkb/proposals").glob("*/proposal.json"):
                contained_paths(root, [path])
                proposal = Proposal.model_validate_json(path.read_text("utf-8"))
                for reference in proposal.candidate_manifest.original_references:
                    retain(reference)
        except (OSError, ValueError, TypeError):
            return ArtifactReferences(frozenset(paths), complete=False)
    return ArtifactReferences(frozenset(paths), complete=True)
