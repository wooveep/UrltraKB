"""Read-only validation of a saved plan against its immutable ledger proofs."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from openkb.agent.document_planning_ledger_integrity import (
    accepted_proofs_valid,
    salvage_proofs_valid,
)
from openkb.sources import SourceStore, content_id, read_object, valid_id


class _ProofCheckpoints:
    def __init__(self, root: Path, identity: dict[str, Any]):
        self.root, self.input = root, identity
        self.records: dict[str, dict[str, Any]] = {}
        self.by_contract: dict[tuple[str, str], str] = {}
        for path in root.glob("*.json"):
            try:
                valid_id(path.stem)
                record = read_object(path)
                if not isinstance(record, dict):
                    continue
                contract = record.get("contract")
                if (
                    not isinstance(contract, dict)
                    or contract.get("input") != identity
                    or record.get("input") != identity
                    or record.get("key") != path.stem
                    or content_id(contract) != path.stem
                    or content_id(record.get("value")) != record.get("value_digest")
                    or not isinstance(contract.get("system"), str)
                    or not isinstance(contract.get("payload"), dict)
                    or not contract["system"].startswith("DocumentPlan ")
                ):
                    continue
                contract_key = (contract["system"], content_id(contract["payload"]))
                self.records[path.stem] = record
                self.by_contract[contract_key] = path.stem
            except (OSError, ValueError, TypeError):
                continue

    def identity(self, system: str, payload: dict[str, Any]) -> str:
        return self.by_contract.get((system, content_id(payload)), "")

    def load(self, key: str) -> Any:
        record = self.records.get(key)
        return record["value"] if record is not None else None


class _LedgerView:
    def __init__(self, db: sqlite3.Connection, key: str, checkpoints: _ProofCheckpoints):
        self.db, self.recovery_key, self.checkpoints = db, key, checkpoints

    def _meta(self, name: str) -> Any:
        row = self.db.execute("SELECT value FROM meta WHERE name = ?", (name,)).fetchone()
        return json.loads(row[0]) if row is not None else None


def verify_accepted_plan(
    kb_dir: Path, plan_ref: dict[str, str], record: dict[str, Any]
) -> None:
    """Reject a self-consistent but unaccepted recovery JSON record."""
    store = SourceStore(kb_dir)
    key = plan_ref["recovery_key"]
    path = store.owned_path(
        store.root / "compilation" / "planning-ledgers" / f"{valid_id(key)}.sqlite3"
    )
    if not path.is_file():
        raise ValueError("External reference plan has no accepted ledger")
    identity = record["input"]
    if not isinstance(identity, dict):
        raise ValueError("External reference plan input is invalid")
    checkpoints = _ProofCheckpoints(store.root / "compilation", identity)
    try:
        db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        with closing(db):
            ledger = _LedgerView(db, key, checkpoints)
            receipts = [
                json.loads(row[0])
                for row in db.execute("SELECT payload FROM receipts ORDER BY sequence")
            ]
            accepted = record["value"]["metadata"].get("accepted_window_receipts")
            references = [
                json.loads(row[0])
                for row in db.execute("SELECT payload FROM external_references ORDER BY key")
            ]
            if (
                ledger._meta("status") != "accepted"
                or accepted != receipts
                or sorted(
                    record["value"].get("external_references", []), key=lambda row: row["key"]
                )
                != references
                or not accepted_proofs_valid(ledger, receipts)
                or not salvage_proofs_valid(ledger, receipts)
            ):
                raise ValueError("External reference plan lacks accepted proof")
    except (sqlite3.Error, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("External reference acceptance proof is invalid") from exc
