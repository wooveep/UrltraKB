"""Retain complete compiler deliveries before validation, independently of publication."""

import hashlib
import json
from uuid import uuid4

from ultrakb.agent.page_response import PageResponseError
from ultrakb.mutation import RecoveryRequired

from .blobs import put_blob, read_bytes, retain_blob
from .checkpoint_store import _json
from .coordination import storage_stage


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
        attributes = put_blob(db, _json(record).encode())
        with db.transaction() as connection:
            work._provenance(connection)
            previous = work._record(db, key, signature)
            connection.execute(
                "INSERT INTO ukb_compile_responses VALUES (?, ?, ?, ?, ?, ?)",
                (identifier, work.work_id, key, signature, body.digest, attributes.digest),
            )
            retain_blob(connection, "compile-response", identifier, "body", body)
            retain_blob(connection, "compile-response", identifier, "metadata", attributes)
            work._write_record(
                connection, key, signature, {**previous, "latest_response": identifier}
            )
    return "compile-response/" + identifier


def retained_response(work, key, signature, protocol, sections, stage):
    with storage_stage(work.root, work.identity, read_only=True) as db:
        previous = work._record(db, key, signature)
        identifier = previous.get("latest_response")
        if identifier is None:
            recovered = provider_delivery(db, work, key, signature)
        else:
            recovered = None
    if identifier is None:
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
    with storage_stage(work.root, work.identity, read_only=True) as db:
        row = db.connection.execute(
            "SELECT content_digest, metadata_digest FROM ukb_compile_responses "
            "WHERE response_id = ? AND work_id = ? AND step_key = ? AND request_digest = ?",
            (identifier, work.work_id, key, signature),
        ).fetchone()
        if row is None:
            raise PageResponseError("compile_checkpoint_invalid", "Saved response identity differs")
        metadata = json.loads(read_bytes(db, row[1]))
        if any(
            metadata.get(name) != expected
            for name, expected in (
                ("stage", stage),
                ("response_protocol", protocol),
                ("expected_sections", list(sections)),
            )
        ):
            raise PageResponseError("compile_checkpoint_invalid", "Saved response protocol differs")
        if metadata.get("code") == "page_truncated_response":
            return None
        return read_bytes(db, row[0]).decode("utf-8"), {
            "record_file": "compile-response/" + identifier,
            "response_file": "blob/" + row[0],
            "response_sha256": row[0],
            "execution_id": metadata["execution_id"],
            "awaiting_validation": metadata.get("received") is True,
        }


def provider_delivery(db, work, key, signature):
    """Recover the exact gateway response if compiler validation never received it."""
    row = db.connection.execute(
        "SELECT r.request_id, r.payload_json, p.response_digest "
        "FROM ukb_compile_invocations i "
        "JOIN ukb_stage_attempts s ON s.work_key = 'compile:' || i.invocation_id "
        "JOIN ukb_usage_requests r ON r.stage_attempt_id = s.stage_attempt_id "
        "LEFT JOIN ukb_response_receipts p ON p.request_id = r.request_id "
        "WHERE i.invocation_id = (SELECT invocation_id FROM ukb_compile_invocations "
        "WHERE work_id = ? AND step_key = ? AND request_digest = ? "
        "ORDER BY sequence DESC LIMIT 1) ORDER BY r.sequence DESC LIMIT 1",
        (work.work_id, key, signature),
    ).fetchone()
    if row is None:
        return None
    if row[2] is None:
        if json.loads(row[1])["send_status"] == "not_sent":
            return None
        raise RecoveryRequired("The exact compiler request needs response receipt recovery")
    try:
        value = json.loads(read_bytes(db, row[2]))
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
    return body.strip(), row[0]


def capture_resolution(work, key, signature, raw, delivered, resolutions):
    record = {
        "policy": "source-resolution-v1",
        "response_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "delivered_sha256": hashlib.sha256(delivered.encode()).hexdigest(),
        "resolutions": resolutions,
    }
    with storage_stage(work.root, work.identity) as db:
        attributes = put_blob(db, _json(record).encode())
        with db.transaction() as connection:
            work._provenance(connection)
            previous = work._record(db, key, signature)
            retain_blob(
                connection,
                "compile-resolution",
                work.work_id,
                key + ":" + attributes.digest,
                attributes,
            )
            work._write_record(
                connection, key, signature, {**previous, "delivery_resolution": attributes.digest}
            )
    return "blob/" + attributes.digest
