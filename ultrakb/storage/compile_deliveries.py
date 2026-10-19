"""Retain complete compiler deliveries before validation, independently of publication."""

import hashlib
import json
from uuid import uuid4

from ultrakb.agent.page_response import PageResponseError
from ultrakb.mutation import RecoveryRequired

from .blobs import put_blob, read_bytes, retain_blob
from .checkpoint_store import _json
from .coordination import storage_stage
from .execution_store import now


def capture_response(work, key, signature, response, metadata):
    if not isinstance(response, str):
        raise ValueError("Compiler response requires complete text")
    identifier = uuid4().hex
    record = {**metadata, "execution_id": work.execution_id, "version": 1}
    record["expected_sections"] = list(record.get("expected_sections", ()))
    record["field_errors"] = list(record.get("field_errors", ()))
    with storage_stage(work.root, work.identity) as db:
        work._provenance(db.connection)
        work._record(db, key, signature)
        body = put_blob(db, response.encode("utf-8"))
        previous, saved = work._record_snapshot(db, key, signature)
        if request_id := record.get("provider_request_attempt_id"):
            invocation = db.connection.execute(
                "SELECT i.invocation_id, i.execution_id FROM ukb_compile_invocations i "
                "JOIN ukb_stage_attempts s ON s.work_key = 'compile:' || i.invocation_id "
                "JOIN ukb_usage_requests r ON r.stage_attempt_id = s.stage_attempt_id "
                "WHERE r.request_id = ? AND i.work_id = ? AND i.step_key = ? "
                "AND i.request_digest = ?",
                (request_id, work.work_id, key, signature),
            ).fetchone()
            if invocation is None:
                raise ValueError("The provider receipt belongs to another compile invocation")
            record["execution_id"] = invocation[1]
        else:
            invocation = db.connection.execute(
                "SELECT invocation_id FROM ukb_compile_invocations WHERE work_id = ? "
                "AND step_key = ? AND request_digest = ? AND execution_id = ? "
                "ORDER BY sequence DESC LIMIT 1",
                (work.work_id, key, signature, work.execution_id),
            ).fetchone()
        invocation_id = invocation[0] if invocation else None
        record["invocation_id"] = invocation_id
        attributes = put_blob(db, _json(record).encode())
        encoded = _json({**saved, "latest_response": identifier}, bounded=True)
        timestamp = now()
        with db.transaction() as connection:
            work._provenance(connection)
            work._require_record_request(connection, key, signature)
            connection.execute(
                "INSERT INTO ukb_compile_responses VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    identifier,
                    work.work_id,
                    key,
                    signature,
                    body.digest,
                    attributes.digest,
                    record["execution_id"],
                    timestamp,
                ),
            )
            retain_blob(connection, "compile-response", identifier, "body", body)
            retain_blob(connection, "compile-response", identifier, "metadata", attributes)
            if invocation_id == saved.get("latest_invocation"):
                work._write_record(connection, key, signature, previous, encoded, required=False)
    return "compile-response/" + identifier


def retained_response(work, key, signature, protocol, sections, stage):
    with storage_stage(work.root, work.identity, read_only=True) as db:
        previous = work._record(db, key, signature)
        identifier = previous.get("latest_response")
        row = db.connection.execute(
            "SELECT content_digest, metadata_digest FROM ukb_compile_responses "
            "WHERE response_id = ? AND work_id = ? AND step_key = ? AND request_digest = ?",
            (identifier, work.work_id, key, signature),
        ).fetchone()
        if identifier is not None and row is None:
            raise PageResponseError("compile_checkpoint_invalid", "Saved response identity differs")
        metadata = json.loads(read_bytes(db, row[1])) if row else None
        if metadata is not None and metadata.get("invocation_id") == previous.get(
            "latest_invocation"
        ):
            if any(
                metadata.get(name) != expected
                for name, expected in (
                    ("stage", stage),
                    ("response_protocol", protocol),
                    ("expected_sections", list(sections)),
                )
            ):
                raise PageResponseError(
                    "compile_checkpoint_invalid", "Saved response protocol differs"
                )
            if metadata.get("code") == "page_truncated_response":
                return None
            return read_bytes(db, row[0]).decode("utf-8"), {
                "record_file": "compile-response/" + identifier,
                "response_file": "blob/" + row[0],
                "response_sha256": row[0],
                "execution_id": metadata["execution_id"],
                "awaiting_validation": metadata.get("received") is True,
            }
        recovered = provider_delivery(db, work, key, signature)
    if recovered is None:
        return None
    body, request_id = recovered
    capture_response(
        work,
        key,
        signature,
        body,
        {
            "stage": stage,
            "response_protocol": protocol,
            "expected_sections": list(sections),
            "received": True,
            "provider_request_attempt_id": request_id,
        },
    )
    return retained_response(work, key, signature, protocol, sections, stage)


def provider_delivery(db, work, key, signature):
    """Recover the exact gateway response if compiler validation never received it."""
    retained = provider_response(db, work, key, signature)
    if retained is None:
        return None
    value, request_id = retained
    try:
        choices = value["choices"]
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError("Expected one complete response choice")
        choice = choices[0]
        if choice.get("finish_reason") != "stop":
            raise ValueError("The retained response did not finish normally")
        body = choice["message"]["content"]
        if not isinstance(body, str):
            raise ValueError("The retained response has no complete text")
    except (ValueError, TypeError, KeyError) as error:
        raise RecoveryRequired("The retained compiler response requires explicit repair") from error
    return body.strip(), request_id


def provider_response(db, work, key, signature):
    """Select only the exact invocation's paid receipt, including truncation status."""
    invocation = db.connection.execute(
        "SELECT invocation_id FROM ukb_compile_invocations "
        "WHERE work_id = ? AND step_key = ? AND request_digest = ? "
        "ORDER BY sequence DESC LIMIT 1",
        (work.work_id, key, signature),
    ).fetchone()
    if invocation is None:
        return None
    work_key = "compile:" + invocation[0]
    row = db.connection.execute(
        "SELECT r.request_id, p.response_digest, r.payload_json FROM ukb_stage_attempts s "
        "JOIN ukb_usage_requests r ON r.stage_attempt_id = s.stage_attempt_id "
        "JOIN ukb_response_receipts p ON p.request_id = r.request_id "
        "WHERE s.work_key = ? ORDER BY r.sequence DESC LIMIT 1",
        (work_key,),
    ).fetchone()
    if row is None:
        uncertain = db.connection.execute(
            "SELECT 1 FROM ukb_stage_attempts s "
            "JOIN ukb_usage_requests r ON r.stage_attempt_id = s.stage_attempt_id "
            "WHERE s.work_key = ? "
            "AND coalesce(json_extract(r.payload_json, '$.send_status'), 'unknown') != 'not_sent' "
            "LIMIT 1",
            (work_key,),
        ).fetchone()
        if uncertain:
            raise RecoveryRequired("The exact compiler request needs response receipt recovery")
        return None
    try:
        value = json.loads(read_bytes(db, row[1]))
    except (ValueError, TypeError, KeyError) as error:
        raise RecoveryRequired("The retained compiler response requires explicit repair") from error
    request = json.loads(row[2])
    if (
        request.get("state") == "failed"
        and isinstance(value, dict)
        and isinstance(value.get("error"), dict)
        and "choices" not in value
    ):
        # A known provider error is retained in the ledger, but is not a model
        # delivery. An explicit continuation still spends the original budget.
        return None
    return value, row[0]


def capture_resolution(work, key, signature, raw, delivered, resolutions):
    record = {
        "execution_id": work.execution_id,
        "policy": "source-resolution-v1",
        "response_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "delivered_sha256": hashlib.sha256(delivered.encode()).hexdigest(),
        "resolutions": resolutions,
    }
    with storage_stage(work.root, work.identity) as db:
        attributes = put_blob(db, _json(record).encode())
        previous, saved = work._record_snapshot(db, key, signature)
        encoded = _json({**saved, "delivery_resolution": attributes.digest}, bounded=True)
        with db.transaction() as connection:
            work._provenance(connection)
            work._require_record_request(connection, key, signature)
            retain_blob(
                connection,
                "compile-resolution",
                work.work_id,
                key + ":" + attributes.digest,
                attributes,
            )
            work._write_record(connection, key, signature, previous, encoded, required=False)
    return "blob/" + attributes.digest
