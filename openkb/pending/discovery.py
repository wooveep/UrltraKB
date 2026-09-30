"""Bounded discovery: one transaction per actual object and its ordinary import intent."""

import hashlib
from pathlib import PurePosixPath

from openkb.locks import LockCancelled, atomic_write_bytes
from openkb.mutation import mutation_scope
from openkb.pending.budget import BudgetWait, DiscoveryMeter
from openkb.pending.formats import recognize
from openkb.pending.ooxml import OOXMLObjects
from openkb.pending.records import DiscoveryCheckpoint, ImportIntent
from openkb.pending.store import group_for, job_records, read_job, save_job
from openkb.source_catalog import read_record, read_source_revision, record_path, write_record
from openkb.state import HashRegistry


def save_budget(kb_dir, intent, meter, error=None):
    if error:
        intent = intent.model_copy(update={"status": "budget_wait", "message": str(error)})
    records = job_records(kb_dir, "discovery", intent)
    records[record_path(kb_dir, "execution-groups", intent.root_import_id)] = meter.updated_group()
    with mutation_scope(
        kb_dir, list(records), operation="discovery-budget-wait" if error else "discovery-scan"
    ):
        for path, record in records.items():
            write_record(path, record)
    return intent


def discover(kb_dir, intent, check_stop):
    revision = read_source_revision(kb_dir, intent.source_revision_id)
    original = kb_dir / intent.original
    if (
        revision.discovery_intent_id != intent.intent_id
        or revision.original != intent.original
        or HashRegistry.hash_file(original) != revision.digest
    ):
        raise ValueError("Discovery original failed its frozen digest check")
    from openkb.pending.policies import discovery_hosts

    if revision.source_format not in discovery_hosts(intent.policy):
        return save_job(kb_dir, "discovery", intent.model_copy(update={"status": "completed"}))
    legacy = intent.policy == "docx-embedded-package-v1"
    from openkb.pending.cfb import CompoundObjects, ContainerRebuildRequired

    provider = CompoundObjects if revision.source_format in {"doc", "xls", "ppt"} else OOXMLObjects
    with provider(original) as package:
        meter = DiscoveryMeter(group_for(kb_dir, intent), check_stop)
        try:
            objects = package.scan(meter, legacy=legacy, policy=intent.policy)
        except BudgetWait as exc:
            return save_budget(kb_dir, intent, meter, exc)
        save_budget(kb_dir, intent, meter)
        for position in range(intent.cursor, len(objects)):
            check_stop()
            group = group_for(kb_dir, intent)
            budget = group.budget
            meter = DiscoveryMeter(group, check_stop)
            candidate = objects[position]
            name = candidate.key
            payload, extension = b"", None
            diagnostic, outcome = candidate.diagnostic, candidate.outcome
            try:
                meter.check()
                if group.cancelled:
                    return save_job(
                        kb_dir, "discovery", intent.model_copy(update={"status": "cancelled"})
                    )
                if not diagnostic:
                    if intent.depth + 1 > budget.max_depth:
                        raise BudgetWait("Discovery depth budget exhausted")
                    if group.sources >= budget.max_sources:
                        raise BudgetWait("Source count budget exhausted")
                    if (
                        candidate.size > budget.max_object_bytes
                        or group.object_bytes + candidate.size > budget.max_total_bytes
                    ):
                        raise BudgetWait("Object byte budget exhausted")
                    payload, filename = package.read(candidate, meter)
                    payload, extension, outcome, diagnostic = recognize(payload, filename, meter)
                    if legacy and extension != "docx":
                        extension, outcome, diagnostic = (
                            None,
                            "private_object",
                            "Not supported by the retained DOCX discovery policy",
                        )
                    meter.check()
            except BudgetWait as exc:
                return save_budget(kb_dir, intent, meter, exc)
            except LockCancelled:
                raise
            except Exception as exc:
                extension = None
                outcome = (
                    "requires_container_rebuild"
                    if isinstance(exc, ContainerRebuildRequired)
                    else "corrupt_object"
                )
                diagnostic = f"Object could not be recovered: {type(exc).__name__}: {exc}"
            digest = hashlib.sha256(payload).hexdigest()
            if digest in (*intent.ancestry, revision.digest):
                diagnostic = "Repeated content on this execution path; recursive cycle stopped"
                outcome = "cycle"
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
                outcome=outcome,
            )
            next_intent = intent.model_copy(update={"cursor": position + 1})
            checkpoint_path = record_path(kb_dir, "discovery-checkpoints", identity)
            if checkpoint_path.exists():
                if (
                    read_record(kb_dir, "discovery-checkpoints", identity, DiscoveryCheckpoint)
                    != checkpoint
                ):
                    raise ValueError("Existing discovery checkpoint belongs to different work")
                # A saved result is authoritative even if a retained cursor predates it.
                intent = save_job(kb_dir, "discovery", next_intent)
                continue
            updated_group = meter.updated_group(len(payload), artifact is not None)
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
