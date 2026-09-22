"""Bind a private DocumentPlan to a completed knowledge publication receipt."""

from __future__ import annotations

import json
from typing import Any

from openkb.agent.document_plan import from_dict, to_dict, validate_plan
from openkb.config import resolve_entity_types
from openkb.lint import list_existing_wiki_targets
from openkb.locks import atomic_write_json
from openkb.sources import SourceStore, content_id, read_object, valid_id

_INTENT_PROTOCOL = "document-publication-intent-v1"
_PROPOSAL_BINDING_FIELD = "document_publication"


def _page_paths(plan: Any) -> dict[str, str]:
    paths = {}
    for page in plan.pages:
        path = page.target or page.name
        if not path.startswith(("concepts/", "entities/")):
            path = f"{page.kind}s/{path}"
        paths[page.key] = f"{path}.md"
    return paths


def _pending(source: Any, parsed: Any, proposal_id: str) -> dict[str, str]:
    return {
        "proposal_id": valid_id(proposal_id),
        "source_id": source.source_id,
        "source_version": source.id,
        "parse_id": parsed.id,
    }


def _receipt_page_paths(receipt: Any, source: Any, parsed: Any) -> set[str]:
    """Return only page paths from a source-bound completed receipt."""

    if not isinstance(receipt, dict) or (
        receipt.get("source_id"),
        receipt.get("source_version"),
        receipt.get("parse_id"),
    ) != (source.source_id, source.id, parsed.id):
        return set()
    pages = receipt.get("pages")
    if isinstance(pages, list) and all(isinstance(path, str) for path in pages):
        return set(pages)
    return set()


def _page_receipts(value: Any) -> dict[str, dict[str, str]]:
    """Read compact per-page proposal receipts retained across partial retries."""

    if not isinstance(value, dict):
        return {}
    fields = {"proposal_id", "source_id", "source_version", "parse_id", "id"}
    return {
        path: dict(receipt)
        for path, receipt in value.items()
        if isinstance(path, str)
        and isinstance(receipt, dict)
        and set(receipt) == fields
        and all(isinstance(receipt.get(field), str) for field in fields)
    }


def _compact_receipt(receipt: dict[str, Any]) -> dict[str, str]:
    """Keep one page's provenance without duplicating every proposal page list."""

    return {
        field: receipt[field]
        for field in ("proposal_id", "source_id", "source_version", "parse_id", "id")
    }


def publication_binding(plan: Any) -> dict[str, str]:
    """Return the immutable plan identity which a formal proposal must retain."""

    recovery_key = plan.metadata.get("recovery_key")
    if not isinstance(recovery_key, str):
        raise ValueError("DocumentPlan is missing its recovery key")
    return {"protocol": _INTENT_PROTOCOL, "recovery_key": valid_id(recovery_key)}


def proposal_binding(document: Any) -> dict[str, str] | None:
    """Read a formal publication binding embedded in an immutable proposal."""

    if not isinstance(document, dict) or _PROPOSAL_BINDING_FIELD not in document:
        return None
    raw = document[_PROPOSAL_BINDING_FIELD]
    try:
        value = json.loads(raw) if isinstance(raw, str) else None
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid formal proposal publication binding") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"protocol", "recovery_key"}
        or value.get("protocol") != _INTENT_PROTOCOL
    ):
        raise ValueError("Invalid formal proposal publication binding")
    try:
        valid_id(value["recovery_key"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid formal proposal publication binding") from exc
    return value


def _save_plan(checkpoints: Any, plan: Any) -> None:
    key = plan.metadata.get("recovery_key")
    if not isinstance(key, str):
        raise ValueError("DocumentPlan is missing its recovery key")
    checkpoints.save_recovery(key, "plan", to_dict(plan))


def _validate_recoverable_plan(
    kb_dir: Any, parsed: Any, settings: dict[str, Any], plan: Any
) -> None:
    """Reject stale or forged formal plans before using their receipt state."""

    targets = plan.metadata.get("catalog_targets")
    if targets is None:
        # Compatibility for an early formal-plan format that did not retain its
        # full planning catalogue. New plans always carry this frozen baseline.
        targets = sorted(list_existing_wiki_targets(kb_dir / "wiki"))
    if not isinstance(targets, list) or not all(isinstance(target, str) for target in targets):
        raise ValueError("DocumentPlan catalog targets are invalid")
    entity_types = plan.metadata.get("entity_types")
    if entity_types is None:
        entity_types = resolve_entity_types(settings)
    if not isinstance(entity_types, list) or not all(
        isinstance(type_, str) for type_ in entity_types
    ):
        raise ValueError("DocumentPlan entity types are invalid")
    validate_plan(
        plan,
        parsed,
        entity_types,
        set(targets),
    )


def _intent_path(kb_dir: Any, proposal_id: str):
    store = SourceStore(kb_dir)
    return store.owned_path(
        store.root / "compilation" / "publication-intents" / f"{valid_id(proposal_id)}.json"
    )


def _intent(source: Any, parsed: Any, plan: Any, proposal_id: str) -> dict[str, Any]:
    """Bind a formal plan recovery record to the proposal before committing it."""

    binding = publication_binding(plan)
    return {
        "protocol": _INTENT_PROTOCOL,
        "status": "pending",
        **_pending(source, parsed, proposal_id),
        "recovery_key": binding["recovery_key"],
    }


def _valid_intent(value: Any) -> dict[str, Any] | None:
    fields = {
        "protocol",
        "status",
        "proposal_id",
        "source_id",
        "source_version",
        "parse_id",
        "recovery_key",
    }
    if not isinstance(value, dict) or set(value) != fields:
        return None
    if value.get("protocol") != _INTENT_PROTOCOL or value.get("status") not in {
        "pending",
        "settled",
    }:
        return None
    try:
        valid_id(value["proposal_id"])
        valid_id(value["source_id"], source=True)
        valid_id(value["source_version"])
        valid_id(value["parse_id"])
        valid_id(value["recovery_key"])
    except (KeyError, TypeError, ValueError):
        return None
    return value


def _settle_intent(kb_dir: Any, intent: dict[str, Any]) -> None:
    """Mark an intent settled only after its DocumentPlan receipt is durable."""

    atomic_write_json(_intent_path(kb_dir, intent["proposal_id"]), {**intent, "status": "settled"})


def prepare_document_publication(
    kb_dir: Any,
    source: Any,
    parsed: Any,
    settings: dict[str, Any],
    plan: Any,
    proposal: Any,
    *,
    bundle: Any = None,
) -> None:
    """Durably record the intended proposal before its atomic publication starts."""

    from openkb.agent.evidence_checkpoints import CompilationCheckpoints

    _validate_recoverable_plan(kb_dir, parsed, settings, plan)
    if proposal_binding(proposal.document) != publication_binding(plan):
        raise ValueError("Formal proposal does not bind its DocumentPlan recovery identity")
    pending = _pending(source, parsed, proposal.id)
    previous = plan.metadata.get("publication_pending")
    if previous not in (None, pending):
        from openkb.knowledge_commit import completed_publication

        if isinstance(previous, dict) and completed_publication(
            kb_dir, source.source_id, previous.get("proposal_id")
        ):
            raise ValueError("DocumentPlan publication intent mismatch")
    plan.metadata["publication_pending"] = pending
    with CompilationCheckpoints(kb_dir, source, parsed, settings, bundle) as checkpoints:
        _save_plan(checkpoints, plan)
    # The separate marker survives a damaged recovery record, so a completed
    # formal proposal can never be mistaken for a legacy publication.
    atomic_write_json(_intent_path(kb_dir, proposal.id), _intent(source, parsed, plan, proposal.id))


def _apply_document_publication(source: Any, parsed: Any, plan: Any, publication: Any) -> None:
    """Apply a completed publication to an in-memory plan before its durable save."""

    expected = _pending(source, parsed, publication.proposal_id)
    pending = plan.metadata.get("publication_pending")
    if pending is not None and pending != expected:
        raise ValueError("DocumentPlan publication receipt does not match its intent")
    published_paths = set(publication.pages)
    page_paths = _page_paths(plan)
    previous_receipt = plan.metadata.get("publication_receipt")
    prior_paths = _receipt_page_paths(previous_receipt, source, parsed)
    prior_page_receipts = _page_receipts(plan.metadata.get("publication_page_receipts"))
    # Older receipts predate the compact page map.  They remain valid for the
    # current proposal transition, so retain their bounded per-page proof now.
    if isinstance(previous_receipt, dict):
        prior_page_receipt = _compact_receipt(previous_receipt)
        prior_page_receipts = {
            **{path: prior_page_receipt for path in prior_paths},
            **prior_page_receipts,
        }
    published = {
        path
        for page in plan.pages
        for path in [page_paths[page.key]]
        if (page.quality == "published" and path in prior_paths)
        or (page.quality == "verified" and path in published_paths)
    }
    receipt = {**expected, "pages": published}
    receipt["pages"] = sorted(receipt["pages"])
    receipt["id"] = content_id(receipt)
    plan.metadata["publication_receipt"] = receipt
    page_receipts = {
        path: previous for path, previous in prior_page_receipts.items() if path in published
    }
    compact = _compact_receipt(receipt)
    page_receipts.update(
        {
            page_paths[page.key]: compact
            for page in plan.pages
            if page.quality == "verified" and page_paths[page.key] in published_paths
        }
    )
    plan.metadata["publication_page_receipts"] = page_receipts
    plan.metadata.pop("publication_pending", None)
    for page in plan.pages:
        if page_paths[page.key] in published_paths and page.quality == "verified":
            page.quality = "published"


def record_document_publication(
    kb_dir: Any,
    source: Any,
    parsed: Any,
    settings: dict[str, Any],
    plan: Any,
    publication: Any,
    *,
    bundle: Any = None,
) -> None:
    """Mark only actually committed verified pages as published and save the receipt."""

    _apply_document_publication(source, parsed, plan, publication)
    from openkb.agent.evidence_checkpoints import CompilationCheckpoints

    with CompilationCheckpoints(kb_dir, source, parsed, settings, bundle) as checkpoints:
        _save_plan(checkpoints, plan)
    _settle_intent(kb_dir, _intent(source, parsed, plan, publication.proposal_id))


def repair_document_publication(
    kb_dir: Any,
    source: Any,
    parsed: Any,
    settings: dict[str, Any],
    *,
    bundle: Any = None,
    proposal_id: str | None = None,
) -> bool:
    """Settle a pre-recorded publication intent after a post-commit write failure."""

    from openkb.knowledge_commit import completed_publication
    from openkb.processing import ProcessingIncomplete

    expected_source = (source.source_id, source.id, parsed.id)
    store = SourceStore(kb_dir)
    compilation = store.owned_path(store.root / "compilation")

    def matches(intent: dict[str, Any]) -> bool:
        return (
            intent["source_id"],
            intent["source_version"],
            intent["parse_id"],
        ) == expected_source and (proposal_id is None or intent["proposal_id"] == proposal_id)

    def load_plan(recovery_key: str):
        path = compilation / "recovery" / f"{recovery_key}-plan.json"
        try:
            record = read_object(path)
            value = record["value"]
            key = valid_id(recovery_key)
            identity = record["input"]
            if (
                not isinstance(record, dict)
                or record.get("key") != key
                or record.get("kind") != "plan"
                or content_id(value) != record.get("digest")
                or not isinstance(identity, dict)
                or (identity.get("source"), identity.get("version"), identity.get("parse"))
                != expected_source
            ):
                raise ValueError("DocumentPlan recovery receipt is invalid")
            plan = from_dict(value)
            _validate_recoverable_plan(kb_dir, parsed, settings, plan)
            return path, record, plan
        except (FileNotFoundError, KeyError, TypeError, ValueError) as exc:
            raise ProcessingIncomplete(
                "document_publication_receipt_pending", "committing"
            ) from exc

    def valid_receipt(receipt: Any, completed: Any) -> bool:
        return (
            isinstance(receipt, dict)
            and receipt.get("proposal_id") == completed.proposal_id
            and receipt.get("source_id") == source.source_id
            and receipt.get("source_version") == source.id
            and receipt.get("parse_id") == parsed.id
            and isinstance(receipt.get("id"), str)
            and len(receipt["id"]) == 64
        )

    def completed_formal_binding():
        """Find a formal proposal through the immutable completed receipt."""

        from openkb.knowledge_commit import completed_publication, load_proposal

        path = store.owned_path(
            store.kb_dir / ".openkb" / "knowledge" / "completed" / f"{source.source_id}.json"
        )
        try:
            receipt = read_object(path)
            saved_proposal = receipt.get("proposal") if isinstance(receipt, dict) else None
            if proposal_id is not None and saved_proposal != proposal_id:
                return None
            completed = completed_publication(kb_dir, source.source_id, saved_proposal)
            if completed is None:
                return None
            proposal = load_proposal(kb_dir, completed.proposal_id)
            if proposal.version_id != source.id or proposal.parse_id != parsed.id:
                return None
            binding = proposal_binding(proposal.document)
            return completed, binding
        except (FileNotFoundError, KeyError, TypeError):
            return None
        except ValueError as exc:
            raise ProcessingIncomplete(
                "document_publication_receipt_pending", "committing"
            ) from exc

    formal = completed_formal_binding()
    if formal is not None and formal[1] is not None:
        completed, binding = formal
        try:
            plan_path, record, plan = load_plan(binding["recovery_key"])
            if plan.metadata.get("recovery_key") != binding["recovery_key"]:
                raise ValueError("DocumentPlan recovery key does not match completed proposal")
            intent = _intent(source, parsed, plan, completed.proposal_id)
            receipt = plan.metadata.get("publication_receipt")
            if valid_receipt(receipt, completed):
                _settle_intent(kb_dir, intent)
                return False
            if plan.metadata.get("publication_pending") != _pending(
                source, parsed, completed.proposal_id
            ):
                raise ValueError("DocumentPlan publication intent is unavailable for recovery")
            _apply_document_publication(source, parsed, plan, completed)
            settled = to_dict(plan)
            atomic_write_json(
                plan_path, {**record, "value": settled, "digest": content_id(settled)}
            )
            _settle_intent(kb_dir, intent)
            return True
        except (OSError, ValueError) as exc:
            raise ProcessingIncomplete(
                "document_publication_receipt_pending", "committing"
            ) from exc

    intents = compilation / "publication-intents"
    for path in sorted(intents.glob("*.json")):
        try:
            path_proposal = valid_id(path.stem)
        except ValueError:
            continue
        if proposal_id is not None and path_proposal != proposal_id:
            continue
        publication = completed_publication(kb_dir, source.source_id, path_proposal)
        if publication is None:
            continue
        try:
            saved_intent = _valid_intent(read_object(path))
        except (FileNotFoundError, KeyError, TypeError, ValueError) as exc:
            raise ProcessingIncomplete(
                "document_publication_receipt_pending", "committing"
            ) from exc
        if saved_intent is None:
            raise ProcessingIncomplete("document_publication_receipt_pending", "committing")
        if not matches(saved_intent):
            continue
        try:
            plan_path, record, plan = load_plan(saved_intent["recovery_key"])
            expected_pending = _pending(source, parsed, publication.proposal_id)
            receipt = plan.metadata.get("publication_receipt")
            if valid_receipt(receipt, publication):
                if saved_intent["status"] != "settled":
                    _settle_intent(kb_dir, saved_intent)
                return False
            if (
                saved_intent["status"] != "pending"
                or plan.metadata.get("publication_pending") != expected_pending
            ):
                raise ValueError("DocumentPlan publication intent changed during recovery")
            _apply_document_publication(source, parsed, plan, publication)
            settled = to_dict(plan)
            atomic_write_json(
                plan_path, {**record, "value": settled, "digest": content_id(settled)}
            )
            _settle_intent(kb_dir, saved_intent)
        except (OSError, ValueError) as exc:
            raise ProcessingIncomplete(
                "document_publication_receipt_pending", "committing"
            ) from exc
        return True
    return False
