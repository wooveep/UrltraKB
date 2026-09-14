"""Parse immutable input versions; navigation never substitutes for source content."""

from __future__ import annotations

import re
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any

from openkb.evidence import BlockDraft, ParseStore, ParseVersion
from openkb.execution_measurement import measure_span
from openkb.ocr.assembly import assembly_profile
from openkb.ocr.backend import PageOcr, create_ocr
from openkb.ocr.config import OcrSettings, parsing_settings
from openkb.ocr.reprocessing import effective_attempts
from openkb.parsing_failures import read_document
from openkb.processing import processing_checkpoint
from openkb.progress import progress_scope
from openkb.sources import SourceStore, SourceVersion


@measure_span("parsing")
def parse_document(
    kb_dir: Path,
    source: SourceVersion,
    *,
    options: dict[str, Any] | None = None,
    force: bool = False,
    resume_ocr: bool = False,
    _budget=None,
    _depth=0,
    page_overrides: dict[int, OcrSettings] | None = None,
) -> ParseVersion:
    selected = parsing_settings(options)
    native_profile = None
    profile: dict[str, Any] = {
        "parser": "openkb-structured-v3",
        "ocr": native_profile or selected.ocr.profile(),
        "ocr_assembly": assembly_profile(selected.ocr.backend),
        "pymupdf": package_version("pymupdf"),
        "mammoth": package_version("mammoth"),
        "markitdown": package_version("markitdown"),
    }
    if source.suffix == ".pptx":
        profile["pptx"] = "openkb-pptx-v2-notes-and-omissions"
        profile["python-pptx"] = package_version("python-pptx")
    if source.suffix == ".xlsx":
        profile["xlsx"] = "openkb-xlsx-v2-row-relations"
        profile["openpyxl"] = package_version("openpyxl")
    if source.suffix == ".docx":
        profile["docx"] = "openkb-docx-v11-image-transcriptions-and-position-gaps"
    if source.suffix == ".pdf":
        profile["pdf"] = "openkb-pdf-v5-conservative-table-context"
    if source.suffix in {".md", ".markdown", ".txt", ".csv"}:
        profile["text"] = "openkb-text-v3-heading-markers"
    store = ParseStore(kb_dir)
    originals = SourceStore(kb_dir)
    retries, overrides = effective_attempts(originals, source, selected.ocr, page_overrides)
    if retries:
        profile["reprocessing"] = {str(page): attempt for page, attempt in retries.items()}
    path = originals.original(source)
    if source.suffix == ".pdf":
        import pymupdf

        try:
            with pymupdf.open(path) as pdf:
                profile["physical_pages"] = pdf.page_count
        except pymupdf.FileDataError:
            # No trustworthy page count exists. The parser below records a
            # document-level omission; never invent a physical-page denominator.
            pass
    if not force:
        with progress_scope("parse_cache"):
            cached = store.find(source, profile)
            # find validates immutable blocks and assets. Quality warnings do not
            # make these bytes stale; explicit reparse/OCR changes request new work.
            pending = False
            if cached is not None and resume_ocr:
                from openkb.ocr.recovery import has_resumable_jobs

                pending = has_resumable_jobs(
                    originals, source, cached, selected.ocr, retries, overrides
                )
            if cached is not None and not pending:
                store.select(source, cached)
                return cached
    processing_checkpoint("parsing")
    if source.suffix == ".pdf":
        from openkb.parsing_pdf import parse_pdf

        ocr = create_ocr(
            originals, source, selected.ocr, native_profile=native_profile, retries=retries
        )
        if overrides:
            ocr = PageOcr(
                ocr,
                {
                    page: create_ocr(originals, source, settings, retries=retries)
                    for page, settings in overrides.items()
                },
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
            blocks, quality = read_document(
                parse_pdf, path, originals, ocr=ocr, force_pages=set(retries), reuse=reuse
            )
            if (
                quality
                and all("page" not in row for row in quality)
                and "physical_pages" in profile
            ):
                reason = ";".join(row["reason"] for row in quality)
                quality = [
                    {"page": page, "status": "needs_review", "reason": reason}
                    for page in range(1, profile["physical_pages"] + 1)
                ]
        finally:
            if ocr is not None:
                ocr.close()
    elif source.suffix == ".docx":
        from openkb.parsing_docx import parse_docx

        docx_ocr = create_ocr(originals, source, selected.ocr, native_profile=native_profile)
        try:
            blocks, quality = read_document(
                parse_docx,
                path,
                originals,
                ocr=docx_ocr,
                _budget=_budget,
                _depth=_depth,
                _source=source,
                _options=options,
                resume_ocr=resume_ocr or force,
            )
        finally:
            if docx_ocr is not None:
                docx_ocr.close()
    elif source.suffix == ".pptx":
        from openkb.parsing_office import parse_pptx

        blocks, quality = read_document(parse_pptx, path, originals)
    elif source.suffix == ".xlsx":
        from openkb.parsing_office import parse_xlsx

        blocks, quality = read_document(parse_xlsx, path, originals)
    else:
        blocks, quality = read_document(parse_text, path, source, originals)
    processing_checkpoint()
    if selected.ocr.policy == "off":
        quality = [
            {**row, "reason": row["reason"].replace("ocr_unavailable", "ocr_disabled")}
            if row.get("page") not in overrides
            else row
            for row in quality
        ]
    if blocks and not any(
        block.kind != "image" and re.sub(r"!\[[^\]]*\]\([^)]*\)", "", block.text).strip()
        for block in blocks
    ):
        if source.suffix == ".pdf":
            quality = [
                {**row, "status": "needs_review", "reason": row["reason"] + ";readable_text_absent"}
                for row in quality
            ]
        else:
            quality.append({"status": "needs_review", "reason": "readable_text_absent"})
    parsed = store.save(source, profile, blocks, quality=quality)
    from openkb.missing_image_reviews import inherit_missing_images

    inherit_missing_images(store, source, parsed)
    store.select(source, parsed)
    return parsed


def parse_text(
    path: Path, source: SourceVersion, store: SourceStore
) -> tuple[list[BlockDraft], list[dict[str, Any]]]:
    kind = "text"
    if source.suffix in {".md", ".markdown", ".txt", ".csv"}:
        from openkb.docx_containers import decode_text

        text = decode_text(path.read_bytes())
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
            if source.suffix != ".txt" and re.match(r"^#{1,6}\s+", line) and not fence:
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
