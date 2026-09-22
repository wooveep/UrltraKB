"""Immutable acceptance proofs for the mutable DocumentPlan planning ledger."""

from __future__ import annotations

import hashlib
import json
from typing import Any

_PROOF_PROTOCOL = "document-plan-ledger-proof-v1"
_PROOF_SYSTEM = "DocumentPlan accepted ledger state"
_BASELINE_PROOF_SYSTEM = "DocumentPlan ledger catalog baseline"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _proof_payload(ledger: Any, sequence: int, receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        "stage": "document-plan-ledger-proof",
        "protocol": _PROOF_PROTOCOL,
        "ledger": ledger.recovery_key,
        "sequence": sequence,
        "receipt": receipt,
        "catalog_baseline": catalog_baseline_digest(ledger),
    }


def catalog_baseline_digest(ledger: Any) -> str:
    """Hash the original catalogue membership, excluding later source additions."""

    digest = hashlib.sha256()
    for row in ledger.db.execute(
        "SELECT target, brief, digest, baseline FROM catalog WHERE baseline = 1 ORDER BY target"
    ):
        digest.update(_json(list(row)).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _baseline_payload(ledger: Any) -> dict[str, Any]:
    return {
        "stage": "document-plan-ledger-baseline-proof",
        "protocol": _PROOF_PROTOCOL,
        "ledger": ledger.recovery_key,
        "identity": ledger._meta("identity"),
        "catalog_baseline": catalog_baseline_digest(ledger),
    }


def save_catalog_baseline_proof(ledger: Any) -> None:
    """Anchor original catalog membership before any planning window can run."""

    payload = _baseline_payload(ledger)
    key = ledger.checkpoints.key(_BASELINE_PROOF_SYSTEM, payload)
    ledger.checkpoints.save(key, payload)


def catalog_baseline_proof_valid(ledger: Any) -> bool:
    """Reject a mutable ledger that relabels baseline pages as source additions."""

    payload = _baseline_payload(ledger)
    key = ledger.checkpoints.identity(_BASELINE_PROOF_SYSTEM, payload)
    return ledger.checkpoints.load(key) == payload


def plan_state_digest(ledger: Any) -> str:
    """Hash the logical plan rows, excluding mutable W/T scheduling controls."""

    digest = hashlib.sha256()
    for table, columns in (
        ("pages", "key, name, target, payload"),
        ("source_only", "key, payload"),
        ("unresolved", "key, status, payload"),
        ("resolutions", "unresolved_key, payload"),
    ):
        digest.update(table.encode("ascii") + b"\n")
        for row in ledger.db.execute(f"SELECT {columns} FROM {table} ORDER BY 1"):
            digest.update(_json(list(row)).encode("utf-8"))
            digest.update(b"\n")
    digest.update(b"overview\n")
    digest.update(_json(ledger._meta("overview")).encode("utf-8"))
    digest.update(b"\n")
    return digest.hexdigest()


def save_accepted_proof(ledger: Any, receipt: dict[str, Any], sequence: int) -> None:
    """Store the accepted prefix's content digest with the immutable checkpoint store.

    The SQLite ledger deliberately supports replacement of future W/T scheduling
    decisions.  Its self-digest consequently cannot distinguish a deliberately
    recomputed corrupt row from a genuine accepted planning delta.  This proof
    is keyed by the immutable accepted model receipt, so recovery can reject a
    mutated aggregate without retaining complete page bodies in memory.
    """

    payload = _proof_payload(ledger, sequence, receipt)
    key = ledger.checkpoints.key(_PROOF_SYSTEM, payload)
    ledger.checkpoints.save(
        key,
        {
            **payload,
            "plan_digest": plan_state_digest(ledger),
        },
    )


def accepted_proofs_valid(ledger: Any, receipts: list[dict[str, Any]]) -> bool:
    """Verify every accepted receipt and the final aggregate plan state."""

    if not catalog_baseline_proof_valid(ledger):
        return False
    if not receipts:
        # Before the first W/T acceptance there is no model receipt to anchor
        # a proof.  The only legitimate mutable plan state is the initialized
        # empty baseline; future W/T subdivision may vary independently.
        if any(
            ledger.db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
            for table in ("pages", "source_only", "unresolved", "resolutions")
        ):
            return False
        expected_overview = {
            "text": "",
            "ranges": [],
            "limitations": [],
            "status": "complete" if ledger._meta("status") == "accepted" else "partial",
        }
        return ledger._meta("overview") == expected_overview

    expected_digest = plan_state_digest(ledger)
    final_digest = None
    for sequence, receipt in enumerate(receipts, start=1):
        payload = _proof_payload(ledger, sequence, receipt)
        key = ledger.checkpoints.identity(_PROOF_SYSTEM, payload)
        proof = ledger.checkpoints.load(key)
        if (
            not isinstance(proof, dict)
            or {key for key in proof if key != "plan_digest"} != set(payload)
            or any(proof.get(key) != value for key, value in payload.items())
            or not isinstance(proof.get("plan_digest"), str)
            or len(proof["plan_digest"]) != 64
        ):
            return False
        final_digest = proof["plan_digest"]
    return final_digest is None or final_digest == expected_digest
