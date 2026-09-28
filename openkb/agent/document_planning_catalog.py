"""Retain a frozen catalogue only across proven outputs of this same source."""

import json

from openkb.agent.document_planning_state import _valid_state
from openkb.knowledge_commit import completed_publication, load_proposal
from openkb.sources import SourceStore, read_object


def retained_catalog(kb, wiki, source, parsed, checkpoints, current):
    """External catalogue changes require new planning; our publication does not.

    This returns only a candidate old catalogue. The caller must still recompute
    the complete planning identity with current rules/configuration and match
    the completed proposal's original recovery key before adopting it.
    """
    store = SourceStore(kb)
    path = store.owned_path(kb / ".openkb/knowledge/completed" / f"{source.source_id}.json")
    try:
        receipt = read_object(path)
        completed = completed_publication(kb, source.source_id, receipt.get("proposal"))
        if completed is None:
            return None
        proposal = load_proposal(kb, completed.proposal_id)
        if (proposal.version_id, proposal.parse_id) != (source.id, parsed.id):
            return None
        from openkb.agent.document_publication import proposal_binding

        binding = proposal_binding(proposal.document)
        if binding is None:
            return None
        key = binding["recovery_key"]
        state = checkpoints.load_recovery(key, "markdown_plan")
        if state is None or not _valid_state(state, []):
            return None
        snapshot = state["planning_snapshot"]
        context = json.loads(snapshot["context_json"])
        old_runtime = context.get("runtime")
        if (
            not old_runtime
            or old_runtime.get("registry_binding") != current["runtime"]["registry_binding"]
        ):
            return None
        # Only our catalogue outputs may change frozen facts; source/policy/config
        # identity is rechecked by the caller before adopting this candidate.
        runtime = dict(old_runtime)
        runtime["catalog_status"] = {
            **runtime["catalog_status"],
            "shown": len(snapshot["catalog"]),
            "omitted": 0,
        }
        previous = {
            "runtime": runtime,
            "catalog_metadata": snapshot.get("catalog_metadata", {}),
            "catalog": snapshot["catalog"],
            "catalog_types": context.get("catalog_types", {}),
            "catalog_targets": [
                target
                for target in state["catalog_targets"]
                if target.startswith(("concepts/", "entities/"))
            ],
        }
        from openkb.source_refs import SourceOwnership
        from openkb.state import HashRegistry

        ownership = SourceOwnership(kb, source.source_id)

        def rows(value):
            entries = {row[0]: tuple(row[1:]) for row in value["catalog"]}
            return {
                target: (
                    entries.get(target),
                    value["catalog_types"].get(target),
                    value.get("catalog_metadata", {}).get(target),
                )
                for target in value["catalog_targets"]
            }

        before, after = rows(previous), rows(current)
        for target in before.keys() | after.keys():
            if (target in before, before.get(target)) == (target in after, after.get(target)):
                continue
            name = target + ".md"
            path = wiki / name
            if name not in ownership.generated or path.is_symlink():
                return None
            actual = HashRegistry.hash_file(path) if path.is_file() else None
            if actual != ownership.generated[name]:
                return None
        return key, previous
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return None
