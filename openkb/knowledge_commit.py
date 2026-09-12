"""Private knowledge generation and atomic, version-bound proposal publication."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

from openkb.evidence import ParseStore, ParseVersion
from openkb.inputs import processing_directory
from openkb.locks import atomic_write_json, atomic_write_text, kb_ingest_lock, kb_read_lock
from openkb.mutation import _copy_file_atomic, mutation_scope
from openkb.processing import processing_checkpoint
from openkb.sources import SourceStore, SourceVersion, content_id, read_object, valid_id
from openkb.state import HashRegistry


def wiki_version(kb_dir: Path) -> dict[str, str]:
    """All bytes, including frontmatter and additions, are generation inputs."""
    result = {}
    root = kb_dir / "wiki"
    if root.is_symlink():
        raise ValueError("Knowledge generation does not follow Wiki symlinks")
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Knowledge generation does not follow Wiki symlinks")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = HashRegistry.hash_file(path)
    return result


def _page_path(kb_dir: Path, name: str) -> Path:
    root = kb_dir / "wiki"
    path = root / name
    if (
        not name
        or path.resolve() == root.resolve()
        or not path.resolve().is_relative_to(root.resolve())
    ):
        raise ValueError("Proposal path escapes the Wiki")
    if (
        path.is_symlink()
        or any(parent.is_symlink() for parent in path.parents if parent.is_relative_to(root))
        or Path(name).as_posix() != name
        or Path(name).is_absolute()
    ):
        raise ValueError("Invalid proposal page path")
    return path


@dataclass(frozen=True)
class KnowledgeProposal:
    id: str
    source_id: str
    version_id: str
    parse_id: str
    config_id: str
    before: dict[str, str]
    changes: dict[str, str | None]
    protected: tuple[str, ...]
    document: dict[str, Any] | None = None
    registry_revision: str | None = None
    replaces: str | None = None

    def __post_init__(self) -> None:
        valid_id(self.source_id, source=True)
        for item in (self.id, self.version_id, self.parse_id, self.config_id):
            valid_id(item)
        if self.registry_revision is not None:
            valid_id(self.registry_revision)
        if self.replaces is not None and (not isinstance(self.replaces, str) or not self.replaces):
            raise ValueError("Invalid previous document identity")
        if self.document is not None:
            if (
                not isinstance(self.document, dict)
                or self.document.get("source_version") != self.version_id
            ):
                raise ValueError("Invalid document projection")
            required = {
                "name",
                "doc_name",
                "type",
                "origin",
                "path",
                "raw_path",
                "source_path",
                "source_id",
                "source_version",
                "parse_id",
                "input_hash",
            }
            if (
                not required <= set(self.document)
                or set(self.document) - required - {"compilation_profile", "compilation_omissions"}
                or not all(isinstance(value, str) for value in self.document.values())
            ):
                raise ValueError("Invalid document projection fields")
            from openkb.compilation_omissions import stored_omissions

            stored_omissions(self.document)
            if (
                self.document["source_id"] != self.source_id
                or self.document["parse_id"] != self.parse_id
            ):
                raise ValueError("Document projection identity mismatch")
        if not isinstance(self.before, dict) or not isinstance(self.changes, dict):
            raise ValueError("Invalid knowledge proposal files")
        for group in (self.before, self.changes):
            for name, digest in group.items():
                if not isinstance(name, str):
                    raise ValueError("Invalid proposal file name")
                if digest is not None:
                    valid_id(digest)
        if not isinstance(self.protected, tuple) or not all(
            name in self.changes for name in self.protected
        ):
            raise ValueError("Invalid manual-change protection scope")
        values = asdict(self)
        values.pop("id")
        if self.id != content_id(values):
            raise ValueError("Knowledge proposal digest mismatch")


def _directory(kb_dir: Path, *parts: str) -> Path:
    store = SourceStore(kb_dir)
    return store.owned_path(store.kb_dir / ".openkb/knowledge" / Path(*parts))


def _baselines(kb_dir: Path) -> dict[str, str]:
    path = _directory(kb_dir, "baselines.json")
    values = read_object(path) if path.exists() else {}
    for name, digest in values.items():
        _page_path(kb_dir, name)
        valid_id(digest)
    return values


def load_proposal(kb_dir: Path, proposal_id: str) -> KnowledgeProposal:
    value = read_object(_directory(kb_dir, "proposals", f"{valid_id(proposal_id)}.json"))
    if not isinstance(value.get("protected"), list):
        raise ValueError("Invalid proposal protection scope")
    proposal = KnowledgeProposal(**{**value, "protected": tuple(value["protected"])})
    if proposal.id != proposal_id:
        raise ValueError("Proposal identity mismatch")
    for name in {*proposal.before, *proposal.changes}:
        _page_path(kb_dir, name)
    return proposal


class KnowledgeWorkspace:
    """A private clone has no write permission or hardlinks to official pages."""

    def __init__(
        self, kb_dir: Path, source: SourceVersion, parsed: ParseVersion, settings: dict[str, Any]
    ):
        self.kb_dir, self.source, self.parsed, self.settings = (
            kb_dir.resolve(),
            source,
            parsed,
            settings,
        )
        self.store = SourceStore(self.kb_dir)
        self.temporary: tempfile.TemporaryDirectory[str] | None = None

    def __enter__(self) -> KnowledgeWorkspace:
        with kb_ingest_lock(self.kb_dir / ".openkb"):
            if not ParseStore(self.kb_dir).compilable(self.source, self.parsed):
                raise ValueError("Source parsing has no usable evidence")
            self.before = wiki_version(self.kb_dir)
            self.baselines = _baselines(self.kb_dir)
            registry = self.kb_dir / ".openkb/hashes.json"
            self.registry_revision = HashRegistry.hash_file(registry) if registry.exists() else None
            self.temporary = processing_directory(prefix="openkb-knowledge-")
            self.path = Path(self.temporary.name)
            try:
                shutil.copytree(self.kb_dir / "wiki", self.path / "wiki")
                atomic_write_text(self.path / ".openkb/config.yaml", yaml.safe_dump(self.settings))
                if (
                    wiki_version(self.path) != self.before
                    or wiki_version(self.kb_dir) != self.before
                ):
                    raise ValueError("Wiki changed during private preparation")
                # Preserve independent, verified bytes before generation can
                # change the private clone or an external editor changes live pages.
                for name, digest in self.before.items():
                    self.store.put_file(_page_path(self.path, name), digest)
            except BaseException:
                self.temporary.cleanup()
                raise
        return self

    def __exit__(self, *args):
        if self.temporary is not None:
            self.temporary.cleanup()

    def proposal(
        self, document: dict[str, Any] | None = None, *, replaces: str | None = None
    ) -> KnowledgeProposal:
        with kb_ingest_lock(self.kb_dir / ".openkb"):
            after = wiki_version(self.path)
            changes = {
                name: after.get(name)
                for name in sorted(self.before.keys() | after.keys())
                if self.before.get(name) != after.get(name)
            }
            for name, digest in changes.items():
                if name in self.before:
                    self.store.asset(self.before[name])
                if digest is not None:
                    self.store.put_file(_page_path(self.path, name), digest)
            from openkb.schema import INDEX_SEED

            protected = []
            for name in changes:
                previous = self.before.get(name)
                if previous is None or self.baselines.get(name) == previous:
                    continue
                # The unedited initialization seed has a known project baseline.
                if name == "index.md" and previous == self.store.put_bytes(
                    INDEX_SEED.encode("utf-8")
                ):
                    continue
                protected.append(name)
            payload: dict[str, Any] = {
                "source_id": self.source.source_id,
                "version_id": self.source.id,
                "parse_id": self.parsed.id,
                "config_id": content_id(self.settings),
                "before": self.before,
                "changes": changes,
                "protected": tuple(protected),
                "document": document,
                "registry_revision": self.registry_revision,
                "replaces": replaces,
            }
            proposal = KnowledgeProposal(id=content_id(payload), **payload)
            path = _directory(self.kb_dir, "proposals", f"{proposal.id}.json")
            with mutation_scope(self.kb_dir, [path], operation="knowledge proposal"):
                if path.exists() and load_proposal(self.kb_dir, proposal.id) != proposal:
                    raise ValueError("Immutable knowledge proposal changed")
                atomic_write_json(path, asdict(proposal))
            return proposal


def _inputs_match(kb_dir: Path, proposal: KnowledgeProposal) -> bool:
    from openkb.ocr.reprocessing import page_attempts

    registry = kb_dir / ".openkb/hashes.json"
    revision = HashRegistry.hash_file(registry) if registry.exists() else None
    source = SourceStore(kb_dir).version(proposal.version_id)
    store = ParseStore(kb_dir)
    parsed = store.load(proposal.parse_id)
    return (
        revision == proposal.registry_revision
        and SourceStore(kb_dir).current(proposal.source_id).id == proposal.version_id
        and wiki_version(kb_dir) == proposal.before
        and store.find(source, parsed.profile) == parsed
        and store.selected(source) == parsed
        and parsed.profile.get("reprocessing", {})
        == {
            str(page): attempt
            for page, attempt in page_attempts(
                store.sources, source, parsed.profile.get("ocr", {})
            ).items()
        }
        and store.compilable(source, parsed)
    )


def accept_proposal(kb_dir: Path, proposal_id: str, pages: list[str], *, config_id: str) -> None:
    """Explicitly accept exactly the protected differences shown by this proposal."""
    with kb_ingest_lock(kb_dir / ".openkb"):
        proposal = load_proposal(kb_dir, proposal_id)
        if config_id != proposal.config_id or not _inputs_match(kb_dir, proposal):
            raise ValueError("Proposal inputs changed; review a fresh proposal")
        if not isinstance(pages, list) or sorted(pages) != sorted(proposal.protected):
            raise ValueError("Acceptance must name exactly the protected differences")
        path = _directory(kb_dir, "accepted", f"{proposal.id}.json")
        with mutation_scope(kb_dir, [path], operation="accept knowledge differences"):
            atomic_write_json(path, {"proposal": proposal.id, "pages": sorted(pages)})


@dataclass(frozen=True)
class Publication:
    status: str
    proposal_id: str
    pages: tuple[str, ...] = ()


def publish_proposal(
    kb_dir: Path,
    proposal_id: str,
    *,
    config_id: str,
    input_is_current: Callable[[], bool] = lambda: True,
) -> Publication:
    with kb_ingest_lock(kb_dir / ".openkb"):
        proposal = load_proposal(kb_dir, proposal_id)
        try:
            current = input_is_current()
        except OSError:
            current = False
        if not current or not _inputs_match(kb_dir, proposal) or (config_id != proposal.config_id):
            return Publication("input_conflict", proposal.id)
        if proposal.protected:
            accepted = _directory(kb_dir, "accepted", f"{proposal.id}.json")
            if not accepted.exists() or read_object(accepted) != {
                "proposal": proposal.id,
                "pages": sorted(proposal.protected),
            }:
                return Publication("needs_acceptance", proposal.id, proposal.protected)
        baselines = _baselines(kb_dir)
        baseline_path = _directory(kb_dir, "baselines.json")
        receipt = _directory(kb_dir, "completed", f"{proposal.source_id}.json")
        paths = [_page_path(kb_dir, name) for name in proposal.changes]
        if proposal.document is not None:
            paths.append(kb_dir / ".openkb/hashes.json")
        store = SourceStore(kb_dir)
        for digest in proposal.changes.values():
            if digest is not None:
                store.asset(digest)
        processing_checkpoint("committing")
        with mutation_scope(
            kb_dir, [*paths, baseline_path, receipt], operation="publish source knowledge"
        ):
            for name, digest in proposal.changes.items():
                processing_checkpoint()
                path = _page_path(kb_dir, name)
                if digest is None:
                    path.unlink(missing_ok=True)
                    baselines.pop(name, None)
                else:
                    _copy_file_atomic(store.asset(digest), path)
                    baselines[name] = digest
            atomic_write_json(baseline_path, baselines)
            if proposal.document is not None:
                registry = HashRegistry(kb_dir / ".openkb/hashes.json")
                if proposal.replaces is not None and proposal.replaces != proposal.source_id:
                    registry.remove_by_hash(proposal.replaces)
                registry.add(proposal.source_id, proposal.document)
            atomic_write_json(
                receipt,
                {
                    "proposal": proposal.id,
                    "source_version": proposal.version_id,
                    "parse": proposal.parse_id,
                },
            )
        return Publication("completed", proposal.id, tuple(proposal.changes))


def proposal_diff(kb_dir: Path, proposal_id: str) -> dict[str, Any]:
    with kb_read_lock(kb_dir / ".openkb"):
        return asdict(load_proposal(kb_dir, proposal_id))
