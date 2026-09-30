"""A committed import intent enters the existing admission/version/publication flow."""

import hashlib

from openkb.application.ingestion import import_prepared_source
from openkb.compilation_report import collect_compile_report
from openkb.inputs import prepared_input
from openkb.pending.records import DerivedExecution, DiscoveryCheckpoint
from openkb.pending.store import save_job
from openkb.source_catalog import admit_source_revision, read_record, read_source_revision
from openkb.source_records import DiscoveryIntent
from openkb.state import HashRegistry


def import_file(kb_dir, job, context):
    path = kb_dir / job.payload
    if HashRegistry.hash_file(path) != job.digest:
        raise ValueError("Import payload failed its frozen digest check")
    checkpoint = read_record(kb_dir, "discovery-checkpoints", job.intent_id, DiscoveryCheckpoint)
    parent = read_record(
        kb_dir, "discovery-intents", checkpoint.discovery_intent_id, DiscoveryIntent
    )
    revision = read_source_revision(kb_dir, parent.source_revision_id)
    expected_id = hashlib.sha256(
        f"{revision.source_revision_id}\0{checkpoint.object_key}\0{parent.policy}\0{job.digest}".encode()
    ).hexdigest()[:32]
    if (
        checkpoint.import_intent_id != job.intent_id
        or checkpoint.digest != job.digest
        or checkpoint.payload != job.payload
        or checkpoint.policy != parent.policy
        or expected_id != job.intent_id
        or parent.intent_id != revision.discovery_intent_id
        or parent.original != revision.original
        or parent.root_import_id != job.root_import_id
        or parent.kb_generation != job.kb_generation
        or parent.depth + 1 != job.depth
        or (*parent.ancestry, revision.digest) != job.ancestry
    ):
        raise ValueError("Import intent does not match its committed discovery checkpoint")
    with (
        prepared_input(path) as prepared,
        context.begin(kb_dir) as credentials,
        collect_compile_report(),
    ):
        admission = admit_source_revision(
            kb_dir,
            prepared,
            identity="recovered:" + job.intent_id,
            name=job.filename,
            check_stop=context.check_stop,
            execution=DerivedExecution(
                root_import_id=job.root_import_id,
                kb_generation=job.kb_generation,
                depth=job.depth,
                ancestry=job.ancestry,
                discovery_policy=checkpoint.policy,
            ),
        )
        job = save_job(
            kb_dir,
            "import",
            job.model_copy(
                update={
                    "source_id": admission.source.source_id,
                    "source_revision_id": admission.revision.source_revision_id,
                }
            ),
        )
        result = import_prepared_source(
            kb_dir,
            prepared,
            admission=admission,
            context=context,
            bundle=credentials,
            on_event=context.on_event,
            retry_confirmed=True,
        )
        return save_job(
            kb_dir,
            "import",
            job.model_copy(
                update={
                    "status": "completed"
                    if result.status in {"added", "skipped"}
                    else result.status,
                    "message": result.message,
                }
            ),
        )
