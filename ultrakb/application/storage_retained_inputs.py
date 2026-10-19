"""Preserve native originals and figures without publishing unreliable knowledge."""

import tempfile
from dataclasses import asdict
from pathlib import Path

from ultrakb.compile_checkpoints import digest
from ultrakb.converter import _sanitize_stem
from ultrakb.import_text import ImportTextAssessment
from ultrakb.ingest_result import ImportUnitOutcome, IngestResult
from ultrakb.locks import atomic_write_json, atomic_write_text
from ultrakb.processing_policy import classify_markdown_tokens
from ultrakb.remote_assets import resolve_resource_policy
from ultrakb.source_records import Record, RecordId

from .retained_documents import capture_retained_content
from .task_stages import task_stage


class RetainedContent(Record):
    source_revision_id: RecordId
    assessment: ImportTextAssessment
    observed_blank: bool


def retain_unreadable_input(
    scope,
    ready,
    context,
    catalog,
    revision,
    assessment,
    download_assets,
    *,
    source_generation,
    reprocessing=None,
):
    from .storage_processing_policy import (
        POLICY_PATH,
        ProcessingPolicy,
        current_processing_policy,
        needs_reprocessing,
    )

    target = catalog.unit_for_source(revision.source_id, key="body")
    forced = reprocessing is not None and needs_reprocessing(catalog, target, reprocessing)
    if target is None or target.source_revision_id != revision.revision_id or forced:
        doc_name = (
            target.doc_name
            if target
            else (f"{_sanitize_stem(ready.source.stem)}-{revision.source_id[:8]}")
        )
        with task_stage(
            context, "import.retain_original.v1", source_revision_id=revision.revision_id
        ):
            with tempfile.TemporaryDirectory(prefix="ultrakb-native-retention-") as temporary:
                directory = Path(temporary)
                resource_policy = (
                    resolve_resource_policy(scope.kb_dir, download_assets)
                    if ready.source.suffix.lower() in {".html", ".htm"}
                    else None
                )
                raw, blank = capture_retained_content(
                    scope.kb_dir,
                    ready,
                    doc_name,
                    directory,
                    context.check_stop,
                    resource_policy,
                )
                context.check_stop()
                retained = RetainedContent(
                    source_revision_id=revision.revision_id,
                    assessment=assessment,
                    observed_blank=blank,
                )
                atomic_write_json(directory / "retention.json", retained.model_dump(mode="json"))
                policy = ProcessingPolicy(
                    values=reprocessing.policy
                    if reprocessing
                    else current_processing_policy(
                        scope, catalog, revision, doc_name, resource_policy=resource_policy
                    ),
                    request_id=reprocessing.request_id if reprocessing else None,
                )
                atomic_write_json(directory / POLICY_PATH, policy.model_dump(mode="json"))
                body = directory / f"{doc_name}.md"
                native = (
                    body
                    if isinstance(raw, str)
                    else body.with_suffix(".content.json" if isinstance(raw, dict) else ".json")
                )
                atomic_write_text(
                    body,
                    raw.get("text", "")
                    if isinstance(raw, dict)
                    else raw
                    if isinstance(raw, str)
                    else "",
                )
                if native != body:
                    atomic_write_json(native, raw)
                fingerprint = digest(
                    {
                        "assessment": asdict(assessment),
                        "source": raw,
                        "observed_blank": blank,
                        "policy": policy.model_dump(mode="json"),
                    }
                )
                processing = catalog.retain_processing(
                    revision.revision_id,
                    {
                        path.relative_to(directory).as_posix(): path
                        for path in directory.rglob("*")
                        if path.is_file()
                    },
                    body_path=body.name,
                    source_path=native.name,
                    source_kind="text"
                    if isinstance(raw, dict)
                    else "markdown"
                    if isinstance(raw, str)
                    else "pages",
                    fingerprint=fingerprint,
                    operation_id=digest([revision.revision_id, "retained-only", fingerprint]),
                    decision=classify_markdown_tokens(0),
                    content_state="empty" if blank else "unreadable",
                    reprocessing_request_id=reprocessing.request_id if reprocessing else None,
                )
                context.check_stop()
                target = catalog.plan_unit(
                    processing.processing_id,
                    key="body",
                    doc_name=doc_name,
                    expected_generation=target.generation if target else 0,
                    expected_source_generation=None if reprocessing else source_generation,
                )
    message = (
        "资料已保存，未生成知识：没有可靠的原生文字；原件及可用原图仍可阅读。已有知识尚未刷新。"
    )
    return IngestResult(
        str(ready.identity),
        "blocked",
        (),
        source_id=revision.source_id,
        source_revision_id=revision.revision_id,
        input_version=ready.digest,
        quality=("import_text_rejected", "pdf_recognition_anomaly") if assessment.findings else (),
        units=(
            ImportUnitOutcome(
                target.unit_id,
                "blocked",
                target.unit_revision_id,
                view_id=scope.view_id,
                key=target.key,
                doc_name=target.doc_name,
                name=ready.source.name,
                message=message,
                completed_stages=("original_retained", "figures_retained"),
            ),
        ),
        message=message,
    )
