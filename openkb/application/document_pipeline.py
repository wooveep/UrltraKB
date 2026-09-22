"""Source intake, reusable parsing and private whole-document knowledge work."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from openkb import frontmatter
from openkb.cancellation import OperationCancelled
from openkb.compilation_report import (
    collect_compile_report,
    report_auxiliary_warning,
    require_complete_compilation,
)
from openkb.config import DEFAULT_CONFIG
from openkb.converter import _sanitize_stem
from openkb.evidence import Evidence, ParseStore, ParseVersion
from openkb.knowledge_commit import KnowledgeWorkspace, publish_proposal
from openkb.locks import LockCancelled, atomic_write_text
from openkb.mutation import RecoveryRequired
from openkb.parsing import parse_document
from openkb.processing import ProcessingIncomplete, processing_checkpoint, processing_scope
from openkb.source_coverage import source_coverage
from openkb.source_request_journal import journal_source_requests
from openkb.sources import SourceStore, SourceVersion, content_id
from openkb.state import HashRegistry


def compile_version(kb_dir, source, settings, **options):
    result = _compile_version(kb_dir, source, settings, **options)
    return finish_compilation(kb_dir, source, settings, result, bundle=options.get("bundle"))


def finish_compilation(kb_dir, source, settings, result, *, bundle=None):
    from openkb.application.source_history import finish_source_result, source_results_deferred

    if source_results_deferred():
        return result
    result = finish_source_result(kb_dir, result)
    if result.knowledge_compilation == "completed" and result.status != "skipped":
        from openkb.application.execution import document_committed

        document_committed(result)
    return result


def _compile_version(
    kb_dir: Path,
    source: SourceVersion,
    settings: dict[str, Any],
    *,
    bundle=None,
    on_event: Callable[[dict], None] = lambda event: None,
    input_is_current: Callable[[], bool] = lambda: True,
    force: bool = False,
    retry_omissions: bool = False,
    document_name: str | None = None,
    replaces: str | None = None,
    parse_only: bool = False,
    plan_only: bool = False,
    force_parse: bool = False,
    page_overrides=None,
):
    from openkb.agent.evidence_checkpoints import publication_settings
    from openkb.application.documents import DocumentResult
    from openkb.runtime.family_budget import register_source_family

    store = SourceStore(kb_dir)
    originals = (str(store.original(source)),)
    parsed = None
    proposal = None
    plan = None
    stage = "parsing"
    with collect_compile_report() as report:
        try:
            # Capacity/profile validation is part of the normal compilation
            # boundary. An unsafe model contract must leave the source intake
            # durable and return the usual actionable configuration result.
            bound_settings = publication_settings(settings, bundle)
            document_settings = {
                **bound_settings,
                "model": bound_settings.get("model", DEFAULT_CONFIG["model"]),
            }
            register_source_family(kb_dir, source)
            with (
                processing_scope(bound_settings) as budget,
                journal_source_requests(store, source, budget),
            ):
                on_event({"stage": stage})
                if retry_omissions and not force_parse:
                    parsed = ParseStore(kb_dir).selected(source)
                    if parsed is not None:
                        for block in parsed.blocks:
                            store.asset(block.blob)
                            for asset in block.assets:
                                store.asset(asset)
                        on_event({"stage": "parsing", "cached": True})
                parsed = parsed or parse_document(
                    kb_dir,
                    source,
                    options=settings.get("parsing"),
                    force=force_parse,
                    # Continue resumes knowledge work against the saved original
                    # interpretation. Only explicit reparse/OCR refreshes it.
                    resume_ocr=False,
                    page_overrides=page_overrides,
                )
                for row in parsed.quality:
                    if row["status"] == "needs_review" or (
                        "docx_conversion_warning:" in row["reason"]
                        or "non_document_attachment_skipped:" in row["reason"]
                        or "docx_attachment_raw_object:" in row["reason"]
                        or "docx_image_ocr_notice:" in row["reason"]
                        or "pdf_image_ocr_notice:" in row["reason"]
                    ):
                        report_auxiliary_warning(row["reason"])
                if ParseStore(kb_dir).accepted_missing_images(source, parsed):
                    report_auxiliary_warning("docx_missing_original_images_accepted")
                if not ParseStore(kb_dir).compilable(source, parsed):
                    raise ProcessingIncomplete("source_quality_needs_review", "parsing")
                if parse_only:
                    return DocumentResult(
                        source.origin,
                        "unfinished",
                        originals,
                        input_version=source.id,
                        source_intake="saved",
                        knowledge_compilation="not_started",
                        stage="parsed",
                        reason="knowledge_compilation_pending",
                        resume=source.id,
                        source_id=source.source_id,
                        parse_id=parsed.id,
                        usage=report.usage,
                        warnings=tuple(report.warnings),
                        coverage=source_coverage(source, parsed, report),
                    )
                registry = HashRegistry(kb_dir / ".openkb/hashes.json")
                previous = registry.get(source.source_id)
                from openkb.compilation_omissions import stored_omissions, validate_omissions
                from openkb.source_coverage import stored_coverage

                previous_omissions = stored_omissions(previous)
                previous_coverage = (
                    stored_coverage(previous, source, parsed)
                    if previous
                    and previous.get("source_version") == source.id
                    and previous.get("parse_id") == parsed.id
                    else {}
                )
                from openkb.agent.document_publication import repair_document_publication

                if repair_document_publication(
                    kb_dir, source, parsed, document_settings, bundle=bundle
                ):
                    on_event({"stage": "committing", "operation": "publication_receipt_recovered"})
                    return DocumentResult(
                        source.origin,
                        "added",
                        originals,
                        input_version=source.id,
                        source_intake="saved",
                        knowledge_compilation="completed",
                        stage="committed",
                        omissions=previous_omissions,
                        resume=source.id
                        if previous_coverage.get("status") in {"partial", "pending"}
                        else None,
                        usage=report.usage,
                        source_id=source.source_id,
                        parse_id=parsed.id,
                        coverage=previous_coverage,
                    )
                has_gaps = bool(previous_omissions) or previous_coverage.get("status") in {
                    "pending",
                    "partial",
                }
                if (
                    not force
                    and not (retry_omissions and has_gaps)
                    and previous
                    and previous.get("source_version") == source.id
                    and previous.get("parse_id") == parsed.id
                    and previous.get("compilation_profile")
                    == bound_settings["_compilation_profile"]
                ):
                    from openkb.agent.evidence_review import stored_review_warnings

                    return DocumentResult(
                        source.origin,
                        "skipped",
                        originals,
                        input_version=source.id,
                        source_intake="saved",
                        knowledge_compilation="completed",
                        stage="committed",
                        warnings=tuple(report.warnings)
                        + stored_review_warnings(previous)
                        + (("knowledge_content_omitted",) if previous_omissions else ()),
                        omissions=previous_omissions,
                        resume=source.id if has_gaps else None,
                        source_id=source.source_id,
                        parse_id=parsed.id,
                        usage=report.usage,
                        coverage=previous_coverage,
                    )
                name = document_name or (previous.get("doc_name") if previous else None)
                name = name or f"{_sanitize_stem(Path(source.name).stem)[:100]}-{source.source_id}"
                if name != _sanitize_stem(name) or len(name) > 200:
                    raise ValueError("Invalid document name")
                from openkb.navigation import prepare_navigation

                navigation = prepare_navigation(
                    kb_dir, source, parsed, bound_settings, bundle=bundle
                )
                if navigation["status"] == "degraded":
                    report_auxiliary_warning("navigation_degraded")
                stage = "compiling"
                on_event({"stage": stage})
                processing_checkpoint(stage)
                with KnowledgeWorkspace(kb_dir, source, parsed, bound_settings) as workspace:
                    cleanup_paths: set[str] = set()
                    _materialize(workspace.path, store, source, parsed, name)
                    from openkb.navigation_tree import snapshot_markdown, snapshot_name

                    atomic_write_text(
                        workspace.path / "wiki" / snapshot_name(source, parsed),
                        snapshot_markdown(
                            kb_dir, source, parsed, asset_root=workspace.path / "wiki/sources"
                        ),
                    )
                    from openkb.agent.compiler import _close_async_llm_clients
                    from openkb.agent.evidence_compiler import compile_evidence

                    try:
                        plan = compile_evidence(
                            kb_dir,
                            workspace.path,
                            source,
                            parsed,
                            name,
                            document_settings,
                            bundle=bundle,
                            on_event=on_event,
                            on_deterministic_cleanup=cleanup_paths.update,
                            navigation=navigation,
                            resume_plan=retry_omissions,
                            allow_planning_omission=replaces is None,
                            plan_only=plan_only,
                        )
                    finally:
                        asyncio.run(_close_async_llm_clients())
                    if plan_only:
                        return DocumentResult(
                            source.origin,
                            "unfinished",
                            originals,
                            input_version=source.id,
                            source_intake="saved",
                            knowledge_compilation="not_started",
                            stage="planned",
                            reason="document_plan_ready",
                            resume=source.id,
                            warnings=tuple(report.warnings),
                            usage=report.usage,
                            source_id=source.source_id,
                            parse_id=parsed.id,
                            coverage=source_coverage(source, parsed, report),
                        )
                    require_complete_compilation()
                    summary = workspace.path / "wiki/summaries" / f"{name}.md"
                    # The complete byte baseline includes these generated metadata.
                    parts = frontmatter.split(summary.read_text(encoding="utf-8"))
                    body = parts[1] if parts else summary.read_text(encoding="utf-8")
                    metadata = parts[0] if parts else frontmatter.block([])
                    for key, value in {
                        "source_id": source.source_id,
                        "source_version": source.id,
                        "parse_id": parsed.id,
                        "title": source.name,
                    }.items():
                        metadata = frontmatter.set_line(metadata, key, value)
                    from openkb.source_omissions import omission_notice

                    body += omission_notice(source, parsed)
                    from openkb.source_summary import finish_source_summary

                    metadata, body = finish_source_summary(metadata, body, source.name)
                    from openkb.compilation_omissions import omission_notice as compilation_notice

                    omissions = validate_omissions(report.omissions)
                    coverage = source_coverage(source, parsed, report, published=True)
                    body += compilation_notice(omissions)
                    from openkb.agent.evidence_review import review_notice

                    body += review_notice(report)
                    # Only this newly generated summary is owned by the source.
                    # Later cross-source link cleanup must not absorb manual text.
                    body = (
                        f"<!-- openkb-source:{source.source_id} -->\n"
                        + body
                        + f"\n<!-- /openkb-source:{source.source_id} -->"
                    )
                    atomic_write_text(summary, metadata + body)
                    document = {
                        "name": source.name,
                        "doc_name": name,
                        "type": source.suffix.lstrip("."),
                        "origin": "url"
                        if source.origin.startswith(("http:", "https:"))
                        else "file",
                        "path": source.origin,
                        "raw_path": store.original(source).relative_to(kb_dir).as_posix(),
                        "source_path": f"wiki/sources/{name}.md",
                        "source_id": source.source_id,
                        "source_version": source.id,
                        "parse_id": parsed.id,
                        "input_hash": source.blob,
                        "navigation_id": navigation["id"],
                        "compilation_navigation_id": navigation["id"],
                        "compilation_profile": bound_settings["_compilation_profile"],
                        "compilation_omissions": json.dumps(omissions, ensure_ascii=False),
                        "compilation_coverage": json.dumps(coverage, ensure_ascii=False),
                        "compilation_review_warnings": json.dumps(
                            [
                                code
                                for code in report.warnings
                                if code.startswith("knowledge_review_")
                            ]
                        ),
                    }
                    if plan is not None:
                        from openkb.agent.document_publication import publication_binding

                        document["document_publication"] = json.dumps(
                            publication_binding(plan), sort_keys=True
                        )
                    proposal = workspace.proposal(
                        document,
                        replaces=replaces,
                        protected_paths=cleanup_paths,
                    )
                    if plan is not None:
                        from openkb.agent.document_publication import prepare_document_publication

                        prepare_document_publication(
                            kb_dir,
                            source,
                            parsed,
                            document_settings,
                            plan,
                            proposal,
                            bundle=bundle,
                        )
                stage = "committing"
                on_event({"stage": stage})
                publication = publish_proposal(
                    kb_dir,
                    proposal.id,
                    config_id=content_id(bound_settings),
                    input_is_current=input_is_current,
                )
                if publication.status != "completed":
                    raise ProcessingIncomplete(publication.status, "committing")
                if plan is not None:
                    from openkb.agent.document_publication import record_document_publication

                    try:
                        record_document_publication(
                            kb_dir,
                            source,
                            parsed,
                            document_settings,
                            plan,
                            publication,
                            bundle=bundle,
                        )
                    except (OSError, ValueError) as exc:
                        # Publication is already durable, but its formal plan
                        # receipt is not.  The pre-publication intent makes a
                        # later Continue repairable without re-generation.
                        raise ProcessingIncomplete(
                            "document_publication_receipt_pending", "committing"
                        ) from exc
                try:
                    on_event({"stage": "committed"})
                except (Exception, OperationCancelled):
                    report_auxiliary_warning("commit_observer_unavailable")
                return DocumentResult(
                    source.origin,
                    "added",
                    originals + tuple(str(kb_dir / "wiki" / name) for name in publication.pages),
                    input_version=source.id,
                    source_intake="saved",
                    knowledge_compilation="completed",
                    stage="committed",
                    warnings=tuple(report.warnings),
                    omissions=omissions,
                    resume=source.id if coverage["status"] != "complete" else None,
                    usage=report.usage,
                    source_id=source.source_id,
                    parse_id=parsed.id,
                    coverage=coverage,
                )
        except ProcessingIncomplete as exc:
            reason, stage, status = exc.reason, exc.stage, "unfinished"
        except (OperationCancelled, LockCancelled):
            reason, status = "stopped", "stopped"
        except RecoveryRequired:
            raise
        except Exception as exc:
            reason = f"{stage}_failed:{type(exc).__name__}"
            status = "unfinished" if report.unfinished else "failed"
            if report.quality:
                reason = report.quality[0]
        quality = (
            tuple(row["reason"] for row in parsed.quality if row["status"] == "needs_review")
            if parsed
            else ()
        )
        return DocumentResult(
            source.origin,
            status,
            originals,
            quality=tuple(dict.fromkeys((*quality, *report.quality))),
            unfinished=tuple(report.unfinished)
            or ((stage,) if status in {"unfinished", "stopped"} else ()),
            input_version=source.id,
            source_intake="saved",
            knowledge_compilation=status,
            stage=stage,
            reason=reason,
            resume=proposal.id if proposal else source.id,
            warnings=tuple(report.warnings),
            omissions=tuple(report.omissions),
            usage=report.usage,
            source_id=source.source_id,
            parse_id=parsed.id if parsed else None,
            coverage=source_coverage(source, parsed, report),
        )


def _materialize(
    workspace: Path,
    store: SourceStore,
    source: SourceVersion,
    parsed: ParseVersion,
    name: str,
) -> Path:
    from openkb.mutation import _copy_file_atomic

    destination = workspace / "wiki/sources" / f"{name}.md"
    attachments = {}
    for block in parsed.blocks:
        for attachment in block.location.get("attachment_files", []):
            attachments[attachment["blob"]] = (
                ".bin"
                if attachment.get("extraction") == "raw_object"
                else Path(attachment["name"]).suffix.lower()
            )
        position = block.location
        while "attachment" in position:
            attachment = position["attachment"]
            attachments[attachment["blob"]] = Path(attachment["name"]).suffix.lower()
            position = attachment["position"]
        if "attachment" in block.location:
            for digest in block.assets:
                attachments.setdefault(digest, ".bin")
    asset_paths = {}
    unrenderable = set()
    for block in parsed.blocks:
        processing_checkpoint()
        for digest in block.assets:
            if digest in asset_paths:
                continue
            if digest in attachments:
                attachment_extension = attachments[digest]
                asset = workspace / "wiki/sources/attachments" / f"{digest}{attachment_extension}"
                _copy_file_atomic(store.asset(digest), asset)
                asset_paths[digest] = "attachments/" + asset.name
                continue
            from PIL import Image

            try:
                with Image.open(store.asset(digest)) as picture:
                    extension = {
                        "PNG": ".png",
                        "JPEG": ".jpg",
                        "GIF": ".gif",
                        "WEBP": ".webp",
                        "BMP": ".bmp",
                    }.get(picture.format or "")
                    if extension is None:
                        raise ValueError("Unrecognized source image format")
                    picture.verify()
            except (OSError, ValueError):
                # Some original drawing formats have no renderable preview. They
                # remain downloadable without preventing compilation of the text.
                asset = workspace / "wiki/sources/attachments" / f"{digest}.bin"
                _copy_file_atomic(store.asset(digest), asset)

                asset_paths[digest] = "attachments/" + asset.name
                unrenderable.add(digest)
                report_auxiliary_warning("source_image_preview_unavailable:" + digest)
                continue
            asset = workspace / "wiki/sources/images" / f"{digest}{extension}"
            _copy_file_atomic(store.asset(digest), asset)
            asset_paths[digest] = "images/" + asset.name

    def original_link(match: re.Match[str]) -> str:
        if match[2] not in unrenderable:
            return match[0]
        return f"[Original image; preview unavailable: {match[1]}](asset:{match[2]})"

    def asset_link(match: re.Match[str]) -> str:
        return asset_paths.get(match[1], match[0])

    text = []
    for block in parsed.blocks:
        processing_checkpoint()
        # Shared headers may refer to a figure owned by a different block. Map
        # context and body through the same validated source-wide asset catalog.
        if "attachment" in block.location:
            from openkb.attachments import filename_text

            attachment = block.location["attachment"]
            content = f"[{filename_text(attachment['name'])}](asset:{attachment['blob']})"
        else:
            content = block.context + "\n" + store.asset(block.blob).read_text(encoding="utf-8")
        content = re.sub(r"!\[([^\]]*)\]\(asset:([0-9a-f]{64})\)", original_link, content)
        content = re.sub(r"asset:([0-9a-f]{64})", asset_link, content)
        reference = Evidence(source.source_id, source.id, parsed.id, block.id)
        text.append(f"<!-- source-evidence: {asdict(reference)} -->\n{content}")
    atomic_write_text(destination, "\n\n".join(text))
    return destination
