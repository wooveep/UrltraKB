"""Bounded package recovery with one atomic cursor, payload and import checkpoint."""

import hashlib
import io
import time
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZipFile

from defusedxml.ElementTree import fromstring

from openkb.locks import LockCancelled, atomic_write_bytes
from openkb.mutation import mutation_scope
from openkb.pending.records import DiscoveryCheckpoint, ImportIntent
from openkb.pending.store import group_for, job_records, read_job, save_job
from openkb.source_catalog import read_source_revision, record_path, write_record
from openkb.state import HashRegistry


class BudgetWait(ValueError):
    pass


def _docx_payload(data, remaining, consume):
    """Verify a complete OOXML word-processing package, including ZIP checksums."""
    try:
        with ZipFile(io.BytesIO(data)) as package:
            names = package.namelist()
            if len(names) != len(set(names)):
                raise ValueError("Ambiguous duplicate package parts")
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                return None, 0
            if sum(part.file_size for part in package.infolist()) > remaining:
                raise BudgetWait("Decompression budget exhausted")
            total = 0
            for part in package.infolist():
                with package.open(part) as stream:
                    while chunk := stream.read(65536):
                        consume(len(chunk))
                        total += len(chunk)
                        if total > remaining:
                            raise BudgetWait("Decompression budget exhausted")
            types = fromstring(package.read("[Content_Types].xml"), forbid_dtd=True)
            if not any(
                entry.get("ContentType")
                == (
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document.main+xml"
                )
                for entry in types
            ):
                return None, total
            return "docx", total
    except BadZipFile:
        return None, 0


def discover(kb_dir, intent, check_stop):
    revision = read_source_revision(kb_dir, intent.source_revision_id)
    original = kb_dir / intent.original
    if (
        revision.discovery_intent_id != intent.intent_id
        or revision.original != intent.original
        or HashRegistry.hash_file(original) != revision.digest
    ):
        raise ValueError("Discovery original failed its frozen digest check")
    if revision.source_format != "docx":
        return save_job(kb_dir, "discovery", intent.model_copy(update={"status": "completed"}))
    with ZipFile(original) as package:
        all_names = package.namelist()
        if len(all_names) != len(set(all_names)):
            raise ValueError("Ambiguous duplicate container parts")
        objects = sorted(
            name
            for name in all_names
            if name.startswith("word/embeddings/") and not name.endswith("/")
        )
        for position in range(intent.cursor, len(objects)):
            check_stop()
            started = time.monotonic()
            group = group_for(kb_dir, intent)
            budget = group.budget
            name = objects[position]
            info = package.getinfo(name)
            decompressed = 0
            data = bytearray()
            diagnostic = None

            def consume(count):
                nonlocal decompressed
                decompressed += count
                check_stop()
                if group.decompressed_bytes + decompressed > budget.max_decompressed_bytes:
                    raise BudgetWait("Decompression budget exhausted")
                if (
                    group.discovery_seconds + time.monotonic() - started
                    >= budget.max_discovery_seconds
                ):
                    raise BudgetWait("Discovery time budget exhausted")

            try:
                if group.cancelled:
                    return save_job(
                        kb_dir, "discovery", intent.model_copy(update={"status": "cancelled"})
                    )
                if intent.depth + 1 > budget.max_depth:
                    raise BudgetWait("Discovery depth budget exhausted")
                if group.sources >= budget.max_sources:
                    raise BudgetWait("Source count budget exhausted")
                if (
                    info.file_size > budget.max_object_bytes
                    or group.object_bytes + info.file_size > budget.max_total_bytes
                ):
                    raise BudgetWait("Object byte budget exhausted")
                if group.decompressed_bytes + info.file_size > budget.max_decompressed_bytes:
                    raise BudgetWait("Decompression budget exhausted")
                if group.discovery_seconds >= budget.max_discovery_seconds:
                    raise BudgetWait("Discovery time budget exhausted")
                with package.open(info) as stream:
                    while chunk := stream.read(65536):
                        consume(len(chunk))
                        data.extend(chunk)
                        if (
                            len(data) > budget.max_object_bytes
                            or group.decompressed_bytes + len(data) > budget.max_decompressed_bytes
                        ):
                            raise BudgetWait("Decompression budget exhausted")
                        if (
                            group.discovery_seconds + time.monotonic() - started
                            >= budget.max_discovery_seconds
                        ):
                            raise BudgetWait("Discovery time budget exhausted")
                payload = bytes(data)
                extension, _ = _docx_payload(
                    payload,
                    budget.max_decompressed_bytes - group.decompressed_bytes - len(payload),
                    consume,
                )
                elapsed = time.monotonic() - started
                if group.discovery_seconds + elapsed >= budget.max_discovery_seconds:
                    raise BudgetWait("Discovery time budget exhausted")
            except BudgetWait as exc:
                intent = intent.model_copy(update={"status": "budget_wait", "message": str(exc)})
                records = job_records(kb_dir, "discovery", intent)
                records[record_path(kb_dir, "execution-groups", group.root_import_id)] = (
                    group.model_copy(
                        update={
                            "decompressed_bytes": group.decompressed_bytes + decompressed,
                            "discovery_seconds": group.discovery_seconds
                            + time.monotonic()
                            - started,
                        }
                    )
                )
                with mutation_scope(kb_dir, list(records), operation="discovery-budget-wait"):
                    for path, record in records.items():
                        write_record(path, record)
                return intent
            except LockCancelled:
                raise
            except Exception as exc:
                payload, extension = bytes(data), None
                elapsed = time.monotonic() - started
                diagnostic = f"Object could not be recovered: {type(exc).__name__}: {exc}"
            digest = hashlib.sha256(payload).hexdigest()
            diagnostic = diagnostic or (
                None
                if extension
                else "Unsupported or incomplete package; no ordinary import created"
            )
            if digest in (*intent.ancestry, revision.digest):
                diagnostic = "Repeated content on this execution path; recursive cycle stopped"
            identity = hashlib.sha256(
                f"{revision.source_revision_id}\0{name}\0{intent.policy}\0{digest}".encode()
            ).hexdigest()[:32]
            artifact = (
                f".openkb/artifacts/{digest}/content.{extension}"
                if extension and not diagnostic
                else None
            )
            checkpoint = DiscoveryCheckpoint(
                checkpoint_id=identity,
                discovery_intent_id=intent.intent_id,
                object_key=name,
                policy=intent.policy,
                payload=artifact,
                digest=digest if artifact else None,
                import_intent_id=identity if artifact else None,
                diagnostic=diagnostic,
            )
            next_intent = intent.model_copy(update={"cursor": position + 1})
            updated_group = group.model_copy(
                update={
                    "sources": group.sources + int(artifact is not None),
                    "object_bytes": group.object_bytes + (len(payload) if artifact else 0),
                    "decompressed_bytes": group.decompressed_bytes + decompressed,
                    "discovery_seconds": group.discovery_seconds + elapsed,
                }
            )
            records = {
                record_path(kb_dir, "discovery-checkpoints", identity): checkpoint,
                record_path(kb_dir, "discovery-intents", intent.intent_id): next_intent,
                record_path(kb_dir, "execution-groups", group.root_import_id): updated_group,
            }
            if artifact:
                records[record_path(kb_dir, "import-intents", identity)] = ImportIntent(
                    intent_id=identity,
                    root_import_id=intent.root_import_id,
                    kb_generation=intent.kb_generation,
                    payload=artifact,
                    digest=digest,
                    filename=PurePosixPath(name).stem + "." + extension,
                    depth=intent.depth + 1,
                    ancestry=(*intent.ancestry, revision.digest),
                )
            paths = list(records)
            target = kb_dir / artifact if artifact else None
            if target and not target.exists():
                paths.append(target)
            elif target and HashRegistry.hash_file(target) != digest:
                raise ValueError("Recovered payload conflicts with frozen artifact")
            with mutation_scope(kb_dir, paths, operation="discovery-checkpoint"):
                check_stop()
                if read_job(kb_dir, intent.intent_id)[1] != intent:
                    raise ValueError("Discovery checkpoint target changed")
                if target and not target.exists():
                    atomic_write_bytes(target, payload)
                for path, record in records.items():
                    write_record(path, record)
                check_stop()
            intent = next_intent
    return save_job(kb_dir, "discovery", intent.model_copy(update={"status": "completed"}))
