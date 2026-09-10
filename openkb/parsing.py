"""Parse immutable input versions; navigation never substitutes for source content."""

from __future__ import annotations

from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any

from openkb.evidence import BlockDraft, ParseStore, ParseVersion
from openkb.ocr.assembly import assembly_profile
from openkb.ocr.backend import create_ocr, default_local_profile
from openkb.ocr.config import parsing_settings
from openkb.ocr.reprocessing import page_attempts
from openkb.processing import processing_checkpoint
from openkb.progress import progress_scope
from openkb.sources import SourceStore, SourceVersion


def parse_document(
    kb_dir: Path,
    source: SourceVersion,
    *,
    options: dict[str, Any] | None = None,
    force: bool = False,
    _budget=None,
    _depth=0,
) -> ParseVersion:
    selected = parsing_settings(options)
    native_profile = (
        default_local_profile(selected.ocr) if source.suffix in {".pdf", ".docx"} else None
    )
    profile = {
        "parser": "openkb-structured-v3",
        "ocr": native_profile or selected.ocr.profile(),
        "ocr_assembly": assembly_profile(selected.ocr.backend),
        "pymupdf": package_version("pymupdf"),
        "mammoth": package_version("mammoth"),
        "markitdown": package_version("markitdown"),
    }
    if source.suffix == ".docx":
        profile["docx"] = "openkb-docx-v5-selective-ocr"
    store = ParseStore(kb_dir)
    originals = SourceStore(kb_dir)
    retries = page_attempts(originals, source, selected.ocr.profile())
    if retries:
        profile["reprocessing"] = {str(page): attempt for page, attempt in retries.items()}
    path = originals.original(source)
    if source.suffix == ".pdf":
        import pymupdf

        with pymupdf.open(path) as pdf:
            profile["physical_pages"] = pdf.page_count
    if not force:
        with progress_scope("parse_cache"):
            cached = store.find(source, profile)
            if cached is not None and store.complete(source, cached):
                store.select(source, cached)
                return cached
    processing_checkpoint("parsing")
    if source.suffix == ".pdf":
        from openkb.parsing_pdf import parse_pdf

        ocr = create_ocr(
            originals, source, selected.ocr, native_profile=native_profile, retries=retries
        )
        previous = store.selected(source)
        reuse = {}
        if previous is not None and (
            {k: v for k, v in previous.profile.items() if k != "reprocessing"}
            == {k: v for k, v in profile.items() if k != "reprocessing"}
        ):
            changed_pages = {
                page
                for page, attempt in retries.items()
                if previous.profile.get("reprocessing", {}).get(str(page)) != attempt
            }
            if changed_pages:
                for row in previous.quality:
                    page = row["page"]
                    if page not in changed_pages:
                        reuse[page] = (
                            [
                                BlockDraft(
                                    originals.asset(b.blob).read_text(encoding="utf-8"),
                                    b.kind,
                                    b.location,
                                    b.assets,
                                    b.context,
                                )
                                for b in previous.blocks
                                if b.location["page"] == page
                            ],
                            row,
                        )
        try:
            blocks, quality = parse_pdf(
                path, originals, ocr=ocr, force_pages=set(retries), reuse=reuse
            )
        finally:
            if ocr is not None:
                ocr.close()
    elif source.suffix == ".docx":
        from openkb.parsing_docx import parse_docx

        docx_ocr = create_ocr(originals, source, selected.ocr, native_profile=native_profile)
        try:
            blocks, quality = parse_docx(
                path,
                originals,
                ocr=docx_ocr,
                _budget=_budget,
                _depth=_depth,
                _source=source,
                _options=options,
            )
        finally:
            if docx_ocr is not None:
                docx_ocr.close()
    else:
        blocks, quality = parse_text(path, source, originals)
    processing_checkpoint()
    parsed = store.save(source, profile, blocks, quality=quality)
    store.select(source, parsed)
    return parsed


def parse_text(
    path: Path, source: SourceVersion, store: SourceStore
) -> tuple[list[BlockDraft], list[dict[str, Any]]]:
    kind = "text"
    if source.suffix in {".md", ".markdown", ".txt", ".csv"}:
        text = path.read_text(encoding="utf-8")
    elif source.suffix == ".json":
        from openkb.legacy_pages import saved_pages_text

        text = saved_pages_text(path)
        kind = "converted"
    else:
        from markitdown import MarkItDown

        with path.open("rb") as stream:
            text = MarkItDown().convert_stream(stream, file_extension=source.suffix).text_content
        kind = "converted"
    quality: list[dict[str, Any]] = []
    assets = []
    for reference, digest in source.assets.items():
        if digest is None:
            quality.append({"status": "needs_review", "reason": "missing_asset:" + reference})
        else:
            store.asset(digest)
            assets.append(digest)
            text = text.replace("](" + reference + ")", "](asset:" + digest + ")")
    blocks = []
    paragraph: list[str] = []
    start = 1
    fence = False
    block_kind = "paragraph"

    def flush():
        if paragraph:
            blocks.append(
                BlockDraft(
                    "\n".join(paragraph), block_kind, {"kind": kind, "line": start}, tuple(assets)
                )
            )
            paragraph.clear()

    lines = text.splitlines()
    with progress_scope("text", len(lines), "lines") as progress:
        for line_number, line in enumerate(lines, 1):
            processing_checkpoint()
            if not paragraph:
                start = line_number
                block_kind = "paragraph"
            if line.startswith(("```", "~~~")):
                fence = not fence
                block_kind = "code"
            if line.startswith("#") and not fence:
                flush()
                start, block_kind = line_number, "heading"
                paragraph.append(line)
                flush()
            elif not line.strip() and not fence:
                flush()
            else:
                paragraph.append(line)
            progress.advance()
    flush()
    if fence:
        quality.append({"status": "needs_review", "reason": "unclosed_code_span"})
    if not blocks:
        quality.append({"status": "needs_review", "reason": "empty_content"})
    return blocks, quality
