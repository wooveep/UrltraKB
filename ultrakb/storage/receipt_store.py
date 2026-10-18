"""Preserve received responses when their independent database acceptance fails."""

import base64
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from ultrakb.locks import atomic_write_text, durable_unlink
from ultrakb.source_records import RecordId

from .blobs import BlobRetiring, _prepare_directory, put_blob, read_bytes, retain_blob
from .coordination import storage_stage
from .database import DatabaseIdentity
from .usage_store import UsageStore, require_request_generation, validate_receipt_fields


class ReceiptPacket(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal[1] = 1
    kb_id: str
    generation: str
    request_id: RecordId
    version: int = Field(ge=0, lt=2**63)
    response_base64: str
    usage: dict

    @field_validator("usage")
    @classmethod
    def check_usage(cls, value):
        validate_receipt_fields(value)
        return value


@dataclass(frozen=True)
class CapturedResponse:
    request_id: str
    requires_repair: bool


@dataclass(frozen=True)
class ReceiptRepair:
    repaired: int
    invalid: int
    remaining: int | None


class ReceiptStore:
    def __init__(self, root: Path, identity: DatabaseIdentity):
        self.root, self.identity = root.resolve(), identity
        self.pending = self.root / ".ultrakb/receipts/pending"

    def _accept(self, request_id, response, version, usage):
        with storage_stage(self.root, self.identity) as db:
            require_request_generation(db, request_id)
            blob = put_blob(db, response)
            with db.transaction() as connection:
                prior = connection.execute(
                    "SELECT response_digest FROM ukb_response_receipts WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                if prior and prior[0] != blob.digest:
                    raise ValueError(
                        "A request attempt cannot receive two different full responses"
                    )
                connection.execute(
                    "INSERT INTO ukb_response_receipts VALUES (?, ?, 'received', NULL) "
                    "ON CONFLICT(request_id) DO NOTHING",
                    (request_id, blob.digest),
                )
                retain_blob(connection, "response-receipt", request_id, "response", blob)
            # Usage and response retention each survive failure of the other commit.
            UsageStore(self.root, self.identity).merge(
                request_id, version=version, **{**usage, "send_status": "sent"}
            )

    def capture(self, request_id: str, response: bytes, *, version: int, usage: dict):
        TypeAdapter(RecordId).validate_python(request_id)
        validate_receipt_fields(usage)
        if type(version) is not int or not 0 <= version < 2**63 or not isinstance(response, bytes):
            raise ValueError("A response requires immutable bytes and a valid receipt version")
        # Keep the actual OS lease through the fallback write, even when SQL failed.
        with storage_stage(self.root, self.identity) as db:
            require_request_generation(db, request_id)
            try:
                self._accept(request_id, response, version, usage)
            except (sqlite3.Error, OSError, BlobRetiring):
                packet = ReceiptPacket(
                    kb_id=self.identity.kb_id,
                    generation=self.identity.generation,
                    request_id=request_id,
                    version=version,
                    response_base64=base64.b64encode(response).decode("ascii"),
                    usage=usage,
                )
                path = self.pending / f"{request_id}-{uuid4().hex}.json"
                _prepare_directory(self.root, path)
                atomic_write_text(path, packet.model_dump_json())
                return CapturedResponse(request_id, requires_repair=True)
        return CapturedResponse(request_id, requires_repair=False)

    def repair(self, *, limit: int = 100, byte_limit: int = 64 * 1024**2) -> ReceiptRepair:
        if type(limit) is not int or not 1 <= limit <= 1000 or byte_limit < 1:
            raise ValueError("Receipt repair requires positive bounded limits")
        repaired = invalid = scanned = total_bytes = 0
        with storage_stage(self.root, self.identity):
            if not self.pending.exists():
                return ReceiptRepair(0, 0, 0)
            with os.scandir(self.pending) as entries:
                for entry in entries:
                    if scanned >= limit or total_bytes + entry.stat().st_size > byte_limit:
                        return ReceiptRepair(repaired, invalid, None)
                    scanned += 1
                    total_bytes += entry.stat().st_size
                    try:
                        if entry.is_symlink() or not entry.is_file():
                            raise ValueError("Unknown pending receipt ownership")
                        packet = ReceiptPacket.model_validate_json(Path(entry.path).read_bytes())
                        if (packet.kb_id, packet.generation) != (
                            self.identity.kb_id,
                            self.identity.generation,
                        ):
                            raise ValueError("Pending receipt belongs to another generation")
                        response = base64.b64decode(packet.response_base64, validate=True)
                        self._accept(packet.request_id, response, packet.version, packet.usage)
                    except (ValueError, KeyError, OSError, sqlite3.Error, BlobRetiring):
                        # Unknown or malformed ownership is retained for explicit repair.
                        invalid += 1
                        continue
                    durable_unlink(Path(entry.path))
                    repaired += 1
        return ReceiptRepair(repaired, invalid, invalid)

    def read(self, request_id: str) -> bytes:
        with storage_stage(self.root, self.identity, read_only=True) as db:
            row = db.connection.execute(
                "SELECT response_digest FROM ukb_response_receipts WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if not row:
                raise KeyError(request_id)
            return read_bytes(db, row[0])
