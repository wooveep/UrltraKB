"""Deterministic body and page-metadata proposals against one immutable page."""

from dataclasses import dataclass

from openkb.agent.page_updates import PageUpdate, UnchangedPage


@dataclass(frozen=True)
class UpdateProposal:
    order: int
    response: dict
    complete_scope: bool = False


@dataclass(frozen=True)
class UpdateConflict:
    blocks: tuple[str, ...]
    batches: tuple[int, ...]
    metadata: bool = False


@dataclass(frozen=True)
class MergedUpdate:
    response: dict
    conflicts: tuple[UpdateConflict, ...]


def merge_proposals(update: PageUpdate, proposals: list[UpdateProposal]) -> MergedUpdate:
    """Collect every proposal before grouping conflicts; arrival order has no meaning."""
    import json

    ordered = sorted(proposals, key=lambda p: p.order)
    if len({p.order for p in ordered}) != len(ordered):
        raise ValueError("Proposal source order must be unique")
    by_block: dict[str, list[tuple[int, dict]]] = {}
    metadata: list[tuple[UpdateProposal, dict]] = []
    base = update.request()
    for proposal in ordered:
        obj = proposal.response
        accepted = update.apply(json.dumps(obj), current=update.original)
        if isinstance(accepted, UnchangedPage):
            continue
        for edit in obj["edits"]:
            by_block.setdefault(edit["block_id"], []).append((proposal.order, edit))
        changes = {
            key: accepted[key]
            for key in ("description", "type")
            if key in accepted and accepted[key] != base[key]
        }
        if changes:
            metadata.append((proposal, changes))
    response: dict = {"status": "updated", "base_sha256": update.digest, "edits": []}
    contributing: set[int] = set()
    grouped: dict[tuple[int, ...], list[str]] = {}
    for block in update.blocks:
        entries = by_block.get(block, [])
        unique = []
        for _, edit in entries:
            if edit not in unique:
                unique.append(edit)
        owners = tuple(sorted({order for order, _ in entries}))
        if (
            len(owners) > 1
            and len(unique) > 1
            and any(edit["operation"] in {"replace", "delete"} for edit in unique)
        ):
            grouped.setdefault(owners, []).append(block)
        else:
            response["edits"].extend(unique)
            contributing.update(owners)
    conflicts = [UpdateConflict(tuple(blocks), owners) for owners, blocks in grouped.items()]
    if metadata:
        candidates = [changes for _, changes in metadata]
        if any(not p.complete_scope for p, _ in metadata) or any(
            candidate != candidates[0] for candidate in candidates[1:]
        ):
            owners = tuple(p.order for p in ordered)
            same = next((c for c in conflicts if c.batches == owners), None)
            if same:
                conflicts.remove(same)
            conflicts.append(UpdateConflict(same.blocks if same else (), owners, True))
        else:
            response.update(candidates[0])
            contributing.update(p.order for p, _ in metadata)
    if conflicts and update.source_ranges is not None:
        # Ranges describe a whole proposal, not individual edits. Resolve every
        # edit coupled to a disputed proposal before accepting any of its ranges.
        components = {}
        for seed in conflicts:
            disputed_owners = set(seed.batches)
            blocks = set(seed.blocks)
            resolve_metadata = seed.metadata
            while True:
                previous = (set(disputed_owners), set(blocks), resolve_metadata)
                for conflict in conflicts:
                    if disputed_owners.intersection(conflict.batches):
                        disputed_owners.update(conflict.batches)
                        blocks.update(conflict.blocks)
                        resolve_metadata |= conflict.metadata
                for block, entries in by_block.items():
                    related = {order for order, _ in entries}
                    if disputed_owners & related:
                        blocks.add(block)
                        disputed_owners.update(related)
                metadata_owners = {p.order for p, _ in metadata}
                if disputed_owners & metadata_owners:
                    resolve_metadata = True
                    disputed_owners.update(metadata_owners)
                if previous == (disputed_owners, blocks, resolve_metadata):
                    break
            response["edits"] = [e for e in response["edits"] if e["block_id"] not in blocks]
            if resolve_metadata:
                for key in ("description", "type"):
                    response.pop(key, None)
            contributing.difference_update(disputed_owners)
            components[tuple(sorted(disputed_owners))] = UpdateConflict(
                tuple(b for b in update.blocks if b in blocks),
                tuple(sorted(disputed_owners)),
                resolve_metadata,
            )
        conflicts = list(components.values())
    if update.source_ranges is not None:
        response["source_ranges"] = sorted(
            {
                r
                for p in ordered
                if p.order in contributing
                for r in p.response.get("source_ranges", [])
            }
        )
    if not response["edits"] and not metadata and not conflicts:
        response = {
            "status": "unchanged",
            "base_sha256": update.digest,
            "reason": "All necessary original batches report the contribution is covered.",
        }
    return MergedUpdate(response, tuple(sorted(conflicts, key=lambda c: (c.batches, c.blocks))))
