"""Authenticate retained inputs and exact outcomes of a source-selected refresh."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from openkb import frontmatter
from openkb.application.sources import _manifest
from openkb.ingest_records import ImportUnit, KnowledgeHead, KnowledgeRevision
from openkb.knowledge_evidence import validated_input
from openkb.source_catalog import read_record, read_source
from openkb.source_changes import effective_input
from openkb.state import HashRegistry
from openkb.unit_publication import read_unit_publication


@dataclass(frozen=True)
class RetainedInput:
    identity: str
    unit: ImportUnit
    directory: Path
    manifest: KnowledgeRevision
    originals: tuple[str, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                path
                for path in (
                    self.manifest.normalized_source,
                    self.manifest.source_map.path if self.manifest.source_map else None,
                )
                if path
            )
        )


def retained_inputs(kb_dir: Path, head: KnowledgeHead, wiki: Path) -> dict[str, RetainedInput]:
    """A completed current input must still own the exact normalized bytes being read."""
    from openkb.source_map import read_source_map

    result = {}
    for unit_id, identity in head.inputs.items():
        revision, frozen = validated_input(kb_dir, identity, head.view_id)
        unit = read_record(kb_dir, "units", unit_id, ImportUnit)
        state = read_unit_publication(kb_dir, unit_id, head.view_id)
        retained = _manifest(kb_dir, state)
        if (
            revision.unit_id != unit_id
            or unit.target_revision_id != identity
            or state.status != "completed"
            or state.target_revision_id != identity
            or state.successful_revision_id != identity
            or retained is None
            or not effective_input(kb_dir, head.view_id, identity)
        ):
            raise ValueError("Complete the current selected source import before refresh")
        directory, manifest = retained
        if manifest.processing is None or manifest.normalized_source is None:
            raise ValueError("Retained input has no authenticated compilation contract")
        item = RetainedInput(
            identity,
            unit,
            directory,
            manifest,
            tuple(sorted({frozen.original, *(a.artifact for a in frozen.assets if a.artifact)})),
        )
        for path in item.paths:
            expected = manifest.generated_baselines.get(path)
            if not expected or any(
                HashRegistry.hash_file(root / path) != expected
                for root in (directory / "wiki", wiki)
            ):
                raise ValueError("Retained normalization changed")
        if manifest.source_map:
            read_source_map(wiki, manifest.source_map, unit.doc_name)
        result[identity] = item
    return result


def summary_owner(page: str, wiki: Path, inputs: dict[str, RetainedInput]) -> str | None:
    """Only the authenticated compiler output of this one source is independent."""
    matches = [item for item in inputs.values() if page == f"summaries/{item.unit.doc_name}.md"]
    if len(matches) != 1 or not (wiki / page).is_file():
        return None
    item = matches[0]
    expected = item.manifest.generated_baselines.get(page)
    if not expected or any(
        not (root / page).is_file() or HashRegistry.hash_file(root / page) != expected
        for root in (wiki, item.directory / "wiki")
    ):
        return None
    metadata = frontmatter.parse((wiki / page).read_text("utf-8"))
    links = metadata.get("sources", [])
    if (
        metadata.get("type") != "Summary"
        or metadata.get("full_text") not in item.paths
        or not isinstance(links, list)
        or any(
            not isinstance(link, str) or link not in {*item.paths, *item.originals}
            for link in links
        )
    ):
        return None
    return item.identity


def retained_reader(wiki, item, *, kb_dir, view_id):
    """Freeze only an original authenticated by the retained normalization manifest."""
    from openkb.agent.source_windows import CompileSourceWindows

    reader = CompileSourceWindows(wiki, item.unit.doc_name, kb_dir=kb_dir, view_id=view_id)
    path = reader.original_path
    if path not in item.paths or hashlib.sha256(
        reader._read(path)
    ).hexdigest() != item.manifest.generated_baselines.get(path):
        raise ValueError("Selected original is outside the retained normalization")
    return reader


def selected_reasons(kb_dir, head, source_ids):
    if not source_ids or len(set(source_ids)) != len(source_ids):
        raise ValueError("Select distinct problem source IDs")
    current = {identity: read_source(kb_dir, identity) for identity in source_ids}
    if any(source.removed or source.contribution_empty for source in current.values()):
        raise ValueError("Selected refresh requires active completed sources")
    result = {}
    for page, reasons in head.needs_refresh.items():
        chosen = tuple(reason for reason in reasons if reason.source_id in current)
        for reason in chosen:
            source = current[reason.source_id]
            if (
                reason.kind != "updated"
                or reason.target_revision_id != source.target_revision_id
                or reason.source_generation != source.target_generation
            ):
                raise ValueError("Pending reason does not match the selected source generation")
        if chosen:
            result[page] = chosen
    return result


def current_page_inputs(kb_dir, head, previous, page, reasons, retained, wiki):
    """Read selected contributions and authenticated linked originals, not broad dependencies."""
    selected = {reason.source_id for reason in reasons}
    wanted = {identity for identity, item in retained.items() if item.unit.source_id in selected}
    old_inputs = {}
    for identity in previous.page_dependencies.get(page, ()):
        unit, _ = validated_input(kb_dir, identity, head.view_id)
        old_inputs.setdefault(unit.unit_id, set()).add(identity)
    if not wanted or selected - {retained[i].unit.source_id for i in wanted}:
        return None
    from openkb.agent.source_windows import CompileSourceWindows

    reader = CompileSourceWindows(wiki, retained[next(iter(wanted))].unit.doc_name)
    try:
        associated = reader._originals(page)
    except (ValueError, UnicodeError):
        return None
    by_path = {path: identity for identity, item in retained.items() for path in item.paths}
    if not associated or set(associated) - by_path.keys():
        return None
    for path in associated:
        identity = by_path[path]
        item = retained[identity]
        prior = old_inputs.get(item.unit.unit_id, set())
        if not prior or (item.unit.source_id not in selected and prior != {identity}):
            # A newer normal original cannot stand in for a page's older basis.
            return None
        wanted.add(identity)
    return tuple(sorted(wanted))


def validate_selection_manifest(proposal):
    """A selected refresh retains every input and every unchanged generated baseline."""
    from openkb.refresh_publication import refresh_baselines

    head = proposal.expected_head
    if proposal.inputs != head.inputs or (
        proposal.candidate_manifest.generated_baselines
        != refresh_baselines(
            head, proposal.expected_pages, proposal.candidate_pages, retain_unchanged=True
        )
    ):
        raise ValueError("Selected refresh changed retained inputs or generated baselines")


def validate_selection(kb_dir, proposal, wiki):
    """Apply the same evidence and generation checks on direct and delayed publication."""
    selection, head = proposal.selection, proposal.expected_head
    if selection is None:
        return
    from openkb.refresh_publication import remaining_refresh

    validate_selection_manifest(proposal)
    remaining_refresh(head, selection)
    retained = retained_inputs(kb_dir, head, wiki)
    reasons = selected_reasons(kb_dir, head, selection.source_ids)
    root = (
        kb_dir / ".openkb/knowledge" / head.view_id / "revisions" / str(head.knowledge_revision_id)
    )
    previous = KnowledgeRevision.model_validate_json((root / "manifest.json").read_text())
    for page, owner in selection.summary_owners.items():
        if summary_owner(page, wiki, retained) != owner:
            raise ValueError("Summary independence proof changed")
        if proposal.candidate_pages[page] != proposal.expected_pages.get(page):
            raise ValueError("Independent summary must retain its exact bytes")
    for page, proof in selection.reconciled_pages.items():
        if not page.startswith(("concepts/", "entities/")) or page not in reasons:
            raise ValueError("Shared page is outside selected pending scope")
        expected = current_page_inputs(kb_dir, head, previous, page, reasons[page], retained, wiki)
        files = {
            path: HashRegistry.hash_file(wiki / path)
            for identity in proof.input_revisions
            for path in retained[identity].paths
        }
        if (
            proof.input_revisions != expected
            or proof.original_files != files
            or proof.base_digest != proposal.expected_pages.get(page)
            or proof.candidate_digest != proposal.candidate_pages.get(page)
        ):
            raise ValueError("Shared page evidence does not match its frozen inputs")
    for page, resolved in selection.resolved.items():
        if resolved != reasons.get(page):
            raise ValueError("Pending resolution lacks its exact selected reasons")
    if "index.md" in selection.resolved:
        if any(
            page != "index.md" and any(r.source_id in selection.source_ids for r in pending)
            for page, pending in remaining_refresh(head, selection).items()
        ):
            raise ValueError("Index cannot certify unfinished selected pages")
    changed = {
        p
        for p in proposal.expected_pages.keys() | proposal.candidate_pages.keys()
        if proposal.expected_pages.get(p) != proposal.candidate_pages.get(p)
    }
    if changed - {*selection.reconciled_pages, "index.md"}:
        raise ValueError("Selected refresh changed an unrelated retained page or asset")
