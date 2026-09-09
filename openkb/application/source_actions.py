"""Version-bound continuation, evidence review and explicit knowledge acceptance."""

from __future__ import annotations

import difflib
from dataclasses import asdict
from pathlib import Path
from typing import Any

from openkb.application.documents import DocumentResult
from openkb.application.execution import ExecutionContext
from openkb.compilation_report import collect_compile_report
from openkb.config import resolve_effective_config
from openkb.evidence import Evidence, EvidenceSlice, ParseStore
from openkb.knowledge_commit import accept_proposal, load_proposal, publish_proposal
from openkb.locks import kb_ingest_lock, kb_read_lock
from openkb.processing import processing_scope
from openkb.sources import SourceStore, content_id


def read_source_evidence(kb_dir: Path, reference: Evidence, *, max_chars: int) -> EvidenceSlice:
    return ParseStore(kb_dir).read(reference, max_chars=max_chars)


def source_page_image(
    kb_dir: Path,
    source_id: str,
    *,
    version_id: str,
    page: int,
    width: int = 1000,
    height: int = 1400,
) -> bytes:
    """Render the selected original PDF page for an explicitly bound human review."""
    import pymupdf

    if (
        type(page) is not int
        or page < 1
        or any(type(n) is not int or not 1 <= n <= 4096 for n in (width, height))
    ):
        raise ValueError("Invalid physical page preview bounds")
    with kb_read_lock(kb_dir / ".openkb"):
        store = SourceStore(kb_dir)
        version = store.version(version_id)
        if version.source_id != source_id or version.suffix != ".pdf":
            raise ValueError("Physical page previews require the selected PDF version")
        with pymupdf.open(store.original(version)) as pdf:
            if page > pdf.page_count:
                raise ValueError("Physical page does not exist")
            original = pdf[page - 1]
            scale = min(width / original.rect.width, height / original.rect.height)
            return original.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes(
                "png"
            )


def inspect_source_parse(
    kb_dir: Path,
    source_id: str,
    *,
    version_id: str,
    parse_id: str,
    offset: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    """Page through evidence identities and quality records without loading full text."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Invalid source review page")
    with kb_read_lock(kb_dir / ".openkb"):
        version = SourceStore(kb_dir).version(version_id)
        parsed = ParseStore(kb_dir).load(parse_id)
        if version.source_id != source_id or parsed.input_key != version.input_key:
            raise ValueError("Source and parsing identities do not match")
        blocks = [
            {
                "id": block.id,
                "order": block.order,
                "kind": block.kind,
                "chars": block.chars,
                "location": block.location,
                "assets": block.assets,
            }
            for block in parsed.blocks[offset : offset + limit]
        ]
        return {
            "source_id": source_id,
            "version_id": version_id,
            "parse_id": parse_id,
            "blocks": blocks,
            "total_blocks": len(parsed.blocks),
            "quality": [
                {
                    **row,
                    "decision": ParseStore(kb_dir).page_decision(version, parsed, row["page"])
                    if "page" in row
                    else None,
                }
                for row in parsed.quality[offset : offset + limit]
            ],
            "total_quality": len(parsed.quality),
            "next_offset": offset + limit
            if offset + limit < max(len(parsed.blocks), len(parsed.quality))
            else None,
        }


def review_source_proposal(
    kb_dir: Path, proposal_id: str, *, page: str | None = None, max_chars: int = 100_000
) -> dict[str, Any]:
    """Return the immutable proposed diff; acceptance names its exact protected scope."""
    if type(max_chars) is not int or max_chars < 1:
        raise ValueError("Difference review requires a positive size limit")
    with kb_read_lock(kb_dir / ".openkb"):
        proposal = load_proposal(kb_dir, proposal_id)
        store = SourceStore(kb_dir)
        names = [page] if page is not None else list(proposal.changes)
        if any(name not in proposal.changes for name in names):
            raise ValueError("Page is not part of this proposal")
        result = asdict(proposal)
        result["protected"] = list(proposal.protected)
        result["diffs"] = {}
        used = 0
        for name in names:
            before = proposal.before.get(name)
            after = proposal.changes[name]
            paths = [store.asset(item) if item else None for item in (before, after)]
            if not name.endswith(".md"):
                difference = f"Binary asset: {before or '(new)'} → {after or '(removed)'}"
            else:
                if sum(path.stat().st_size for path in paths if path) > max_chars * 4:
                    raise ValueError("Difference exceeds review limit; select a page or raise it")
                texts = [path.read_text(encoding="utf-8") if path else "" for path in paths]
                difference = "".join(
                    difflib.unified_diff(
                        texts[0].splitlines(keepends=True),
                        texts[1].splitlines(keepends=True),
                        fromfile=name + " (current)",
                        tofile=name + " (proposed)",
                    )
                )
            used += len(difference)
            if used > max_chars:
                raise ValueError("Difference exceeds review limit; select a page or raise it")
            result["diffs"][name] = difference
        return result


def continue_source(
    kb_dir: Path,
    source_id: str,
    *,
    version_id: str,
    proposal_id: str | None = None,
    accept_pages: list[str] | None = None,
    context: ExecutionContext | None = None,
) -> DocumentResult:
    """Continue an already saved version, including after the input file disappeared."""
    from openkb.application.document_pipeline import compile_version

    kb_dir = kb_dir.resolve()
    context = context or ExecutionContext()
    with kb_ingest_lock(kb_dir / ".openkb", cancelled=context.cancelled, on_wait=context.waiting):
        store = SourceStore(kb_dir)
        source = store.current(source_id)
        if source.id != version_id:
            raise ValueError("Source version changed; refresh the source before continuing")
        with context.begin(kb_dir) as bundle:
            settings = resolve_effective_config(kb_dir)[0]
            if proposal_id is None:
                if accept_pages is not None:
                    raise ValueError("Acceptance requires the reviewed proposal identity")
                return compile_version(
                    kb_dir, source, settings, bundle=bundle, on_event=context.on_event
                )
            proposal = load_proposal(kb_dir, proposal_id)
            if proposal.source_id != source.source_id or proposal.version_id != source.id:
                raise ValueError("Proposal does not belong to the selected source version")
            from openkb.agent.evidence_checkpoints import publication_settings

            config_id = content_id(publication_settings(settings, bundle))
            if accept_pages is not None:
                accept_proposal(kb_dir, proposal.id, accept_pages, config_id=config_id)
            with collect_compile_report() as report, processing_scope(settings):
                publication = publish_proposal(kb_dir, proposal.id, config_id=config_id)
            complete = publication.status == "completed"
            result = DocumentResult(
                source.origin,
                "added" if complete else "unfinished",
                (str(store.original(source)),)
                + tuple(str(kb_dir / "wiki" / page) for page in publication.pages if complete),
                input_version=source.id,
                source_id=source.source_id,
                parse_id=proposal.parse_id,
                source_intake="saved",
                knowledge_compilation="completed" if complete else "unfinished",
                stage="committed" if complete else "committing",
                reason=None if complete else publication.status,
                resume=None if complete else proposal.id,
                usage=report.usage,
            )
            from openkb.application.document_pipeline import finish_compilation

            return finish_compilation(kb_dir, source, settings, result, bundle=bundle)


def confirm_source_page(
    kb_dir: Path,
    source_id: str,
    *,
    version_id: str,
    parse_id: str,
    page: int,
    reason: str,
    context: ExecutionContext | None = None,
) -> None:
    kb_dir = kb_dir.resolve()
    context = context or ExecutionContext()
    with kb_ingest_lock(kb_dir / ".openkb", cancelled=context.cancelled, on_wait=context.waiting):
        with context.begin(kb_dir):
            version = SourceStore(kb_dir).current(source_id)
            if version.id != version_id:
                raise ValueError("Source version changed; review the current page")
            store = ParseStore(kb_dir)
            parsed = store.load(parse_id)
            if store.selected(version) != parsed or store.find(version, parsed.profile) != parsed:
                raise ValueError("Parse version changed; review the current page")
            store.confirm_page(version, parsed, page, reason)


def reprocess_source_page(
    kb_dir: Path,
    source_id: str,
    *,
    version_id: str,
    parse_id: str,
    page: int,
    acknowledge_unknown: bool = False,
    context: ExecutionContext | None = None,
) -> DocumentResult:
    """Create one explicit page attempt and refresh parsing with other pages reused."""
    from openkb.application.document_pipeline import compile_version
    from openkb.ocr.config import parsing_settings
    from openkb.ocr.reprocessing import request_page_attempt

    if type(page) is not int or page < 1 or type(acknowledge_unknown) is not bool:
        raise ValueError("Invalid page reprocessing decision")
    kb_dir = kb_dir.resolve()
    context = context or ExecutionContext()
    with kb_ingest_lock(kb_dir / ".openkb", cancelled=context.cancelled, on_wait=context.waiting):
        with context.begin(kb_dir) as bundle:
            sources = SourceStore(kb_dir)
            source = sources.current(source_id)
            parses = ParseStore(kb_dir)
            parsed = parses.load(parse_id)
            if (
                source.id != version_id
                or source.suffix != ".pdf"
                or parses.selected(source) != parsed
            ):
                raise ValueError("Source or parsing changed; review the current physical page")
            if not any(row.get("page") == page for row in parsed.quality):
                raise ValueError("Physical page is outside the reviewed parsing view")
            settings = resolve_effective_config(kb_dir)[0]
            ocr = parsing_settings(settings.get("parsing")).ocr
            if ocr.profile() != parsed.profile.get("ocr"):
                raise ValueError("OCR settings changed; reparse the source before selecting a page")
            selected = ocr.local if ocr.backend == "local" else ocr.cloud
            if selected is None:
                raise ValueError("Configure the selected OCR runtime before reprocessing")
            request_page_attempt(
                sources,
                source,
                ocr.profile(),
                page=page,
                parse_id=parse_id,
                acknowledge_unknown=acknowledge_unknown,
            )
            return compile_version(
                kb_dir,
                source,
                settings,
                bundle=bundle,
                on_event=context.on_event,
                parse_only=True,
                force_parse=True,
            )


def reparse_source(
    kb_dir: Path, source_id: str, *, version_id: str, context: ExecutionContext | None = None
) -> DocumentResult:
    """Refresh parsing from retained bytes; existing knowledge and references remain valid."""
    from openkb.application.document_pipeline import compile_version

    kb_dir = kb_dir.resolve()
    context = context or ExecutionContext()
    with kb_ingest_lock(kb_dir / ".openkb", cancelled=context.cancelled, on_wait=context.waiting):
        source = SourceStore(kb_dir).current(source_id)
        if source.id != version_id:
            raise ValueError("Source changed; review the current version before parsing")
        with context.begin(kb_dir) as bundle:
            return compile_version(
                kb_dir,
                source,
                resolve_effective_config(kb_dir)[0],
                bundle=bundle,
                on_event=context.on_event,
                parse_only=True,
                force_parse=True,
            )


def rebuild_source_navigation(
    kb_dir: Path,
    source_id: str,
    *,
    version_id: str,
    parse_id: str,
    context: ExecutionContext | None = None,
) -> dict[str, Any]:
    """Enhance one retained parse without OCR or changes to published knowledge."""
    from openkb.navigation import build_navigation

    kb_dir = kb_dir.resolve()
    context = context or ExecutionContext()
    with kb_ingest_lock(kb_dir / ".openkb", cancelled=context.cancelled, on_wait=context.waiting):
        with context.begin(kb_dir) as bundle:
            source = SourceStore(kb_dir).version(version_id)
            parsed = ParseStore(kb_dir).load(parse_id)
            if source.source_id != source_id:
                raise ValueError("Source version does not belong to the selected source")
            if not ParseStore(kb_dir).complete(source, parsed):
                raise ValueError("Navigation requires complete, reviewed source evidence")
            context.on_event({"stage": "navigation"})
            return build_navigation(
                kb_dir, source, parsed, resolve_effective_config(kb_dir)[0], bundle=bundle
            )
