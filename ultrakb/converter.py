"""Document conversion pipeline for UltraKB."""

from __future__ import annotations

import logging
import re
import shutil
import unicodedata
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pymupdf

from ultrakb.config import resolve_effective_config
from ultrakb.images import convert_pdf_with_images, extract_base64_images
from ultrakb.inputs import PreparedInput
from ultrakb.locks import (
    PrivateMaterialization,
    atomic_write_json,
    atomic_write_text,
)
from ultrakb.processing_policy import ProcessingDecision, classify_pdf
from ultrakb.source_records import TextDecoding

logger = logging.getLogger(__name__)


@dataclass
class ConvertResult:
    """Result returned by :func:`convert_document`."""

    raw_path: Path | None = None
    source_path: Path | None = None
    is_long_doc: bool = False
    skipped: bool = False
    file_hash: str | None = None  # For deferred hash registration
    doc_name: str | None = None  # Stable wiki name (collision-resistant)
    source_identity: str | None = None  # Frozen alongside the prepared bytes
    processing: ProcessingDecision | None = None
    pdf_path: Path | None = None
    office_path: Path | None = None
    cnki_path: Path | None = None


def _portable_path(resolved_path: Path, resolved_kb: Path) -> str:
    """Format already resolved identities without consulting live symlinks."""
    if resolved_path.is_relative_to(resolved_kb):
        return resolved_path.relative_to(resolved_kb).as_posix()
    return resolved_path.as_posix()


_SAFE_STEM_RE = re.compile(r"[^\w\-]+")
_SUFFIX_LEN = 8


def _sanitize_stem(stem: str) -> str:
    normalized = unicodedata.normalize("NFKC", stem)
    return _SAFE_STEM_RE.sub("-", normalized).strip("-") or "document"


def get_pdf_page_count(path: Path) -> int:
    """Return the number of pages in the PDF at *path* using pymupdf."""
    with pymupdf.open(str(path)) as doc:
        return doc.page_count


def convert_document(
    src: Path,
    kb_dir: Path,
    *,
    staging_dir: Path | None = None,
    prepared: PreparedInput | None = None,
    doc_name: str | None = None,
    decoding: TextDecoding | None = None,
    resource_policy=None,
    check_stop: Callable[[], None] = lambda: None,
    office_identity=None,
    cnki_identity=None,
    private_workspace: PrivateMaterialization | None = None,
) -> ConvertResult:
    """Convert a fixed input version while retaining its original identity."""
    if (
        private_workspace is None
        or staging_dir is None
        or doc_name is None
        or not private_workspace.permits_scope(staging_dir)
    ):
        raise ValueError(
            "Private conversion requires its owned workspace and explicit document name"
        )
    from ultrakb.inputs import prepared_input

    if prepared is not None:
        return _convert_prepared_document(
            src,
            kb_dir,
            staging_dir=staging_dir,
            ready=prepared,
            doc_name=doc_name,
            decoding=decoding,
            resource_policy=resource_policy,
            check_stop=check_stop,
            office_identity=office_identity,
            cnki_identity=cnki_identity,
            private_workspace=private_workspace,
        )
    with prepared_input(src) as ready:
        return _convert_prepared_document(
            src,
            kb_dir,
            staging_dir=staging_dir,
            ready=ready,
            doc_name=doc_name,
            decoding=decoding,
            resource_policy=resource_policy,
            check_stop=check_stop,
            office_identity=office_identity,
            cnki_identity=cnki_identity,
            private_workspace=private_workspace,
        )


def _convert_prepared_document(
    src: Path,
    kb_dir: Path,
    *,
    staging_dir: Path | None = None,
    ready: PreparedInput,
    doc_name: str | None = None,
    decoding: TextDecoding | None = None,
    resource_policy=None,
    check_stop: Callable[[], None] = lambda: None,
    office_identity=None,
    cnki_identity=None,
    private_workspace: PrivateMaterialization | None = None,
) -> ConvertResult:
    """Convert retained input bytes into an owned private materialization."""
    prepared, file_hash = ready.path, ready.digest
    from ultrakb.import_text import preflight_import_text, validate_text_preflight

    validate_text_preflight(
        ready,
        preflight_import_text(kb_dir, ready, check_stop=check_stop, cnki_identity=cnki_identity),
    )
    source_identity = _portable_path(ready.identity, kb_dir.resolve())
    with nullcontext():
        # ------------------------------------------------------------------
        # Load config & state
        # ------------------------------------------------------------------
        config = resolve_effective_config(kb_dir)[0]
        processing = None
        artifact_root = staging_dir if staging_dir is not None else kb_dir
        if not isinstance(doc_name, str) or _sanitize_stem(doc_name) != doc_name:
            raise ValueError("Invalid normalized document name")

        # ------------------------------------------------------------------
        # 2. Copy to raw/
        # ------------------------------------------------------------------
        raw_dir = artifact_root / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        if staging_dir is None and ready.identity.is_relative_to(raw_dir.resolve()):
            # Watch mode: the file already lives in raw/ — don't copy/rename.
            raw_dest = ready.source
        else:
            raw_dest = raw_dir / f"{doc_name}{src.suffix.lower()}"
            shutil.copy2(prepared, raw_dest)

        # ------------------------------------------------------------------
        # 3. PDF long-doc detection
        # ------------------------------------------------------------------
        from ultrakb.inputs import CNKI_SOURCE_EXTENSIONS, OFFICE_SOURCE_EXTENSIONS

        pdf_input = prepared if src.suffix.lower() == ".pdf" else None
        internal_pdf = office_path = cnki_path = None
        if src.suffix.lower() in OFFICE_SOURCE_EXTENSIONS:
            from ultrakb.office.convert import convert_office

            internal_pdf = raw_dir / f"{doc_name}.pdf"
            office_path = convert_office(
                kb_dir,
                prepared,
                internal_pdf,
                check_stop=check_stop,
                expected_identity=office_identity,
            )
            pdf_input = internal_pdf
        elif src.suffix.lower() in CNKI_SOURCE_EXTENSIONS:
            from ultrakb.cnki.convert import prepare_cnki
            from ultrakb.locks import atomic_write_bytes

            pdf, record = prepare_cnki(
                kb_dir, ready, check_stop=check_stop, expected_identity=cnki_identity
            )
            internal_pdf = raw_dir / f"{doc_name}.pdf"
            cnki_path = internal_pdf.with_suffix(".cnki.json")
            atomic_write_bytes(internal_pdf, pdf.read_bytes())
            atomic_write_bytes(cnki_path, record.read_bytes())
            pdf_input = internal_pdf
        if pdf_input is not None:
            page_count = get_pdf_page_count(pdf_input)
            processing = classify_pdf(page_count, config)
            if processing.execution_mode == "segmented":
                logger.info(
                    "PDF uses segmented compilation (%d physical pages): %s",
                    page_count,
                    src.name,
                )
                return ConvertResult(
                    raw_path=raw_dest,
                    is_long_doc=True,
                    file_hash=file_hash,
                    doc_name=doc_name,
                    source_identity=source_identity,
                    processing=processing,
                    pdf_path=internal_pdf,
                    office_path=office_path,
                    cnki_path=cnki_path,
                )

        # ------------------------------------------------------------------
        # 4/5. Convert to Markdown
        # ------------------------------------------------------------------
        sources_dir = artifact_root / "wiki" / "sources"
        sources_dir.mkdir(parents=True, exist_ok=True)
        images_dir = artifact_root / "wiki" / "sources" / "images" / doc_name
        images_dir.mkdir(parents=True, exist_ok=True)

        from ultrakb.inputs import TEXT_SOURCE_EXTENSIONS

        if src.suffix.lower() in TEXT_SOURCE_EXTENSIONS:
            from ultrakb.processing_policy import classify_markdown_tokens
            from ultrakb.text_formats import normalize_text_document

            if src.suffix.lower() in {".html", ".htm"} and resource_policy is None:
                from ultrakb.remote_assets import resolve_resource_policy

                resource_policy = resolve_resource_policy(kb_dir)
            frozen = normalize_text_document(
                ready,
                doc_name,
                artifact_root / "wiki",
                decoding=decoding,
                resource_policy=resource_policy,
            )
            markdown = frozen.text
            processing = classify_markdown_tokens(frozen.tokens)
            atomic_write_json(
                sources_dir / f"{doc_name}.content.json",
                frozen.model_dump(mode="json"),
                ensure_ascii=False,
            )
        elif pdf_input is not None:
            # Use pymupdf dict-mode for PDFs: text + images inline at correct positions
            page_records: list[dict] = []
            markdown = convert_pdf_with_images(
                pdf_input,
                doc_name,
                images_dir,
                page_records=page_records,
                **({"allow_empty_text": True} if office_path else {}),
            )
            if office_path:
                from ultrakb.office.slide_content import attach_slides, read_slides
                from ultrakb.source_contract import note_relative_pages
                from ultrakb.source_pages import read_page_selection

                slides = read_slides(office_path)
                if slides:
                    page_records = attach_slides(page_records, slides)
                    markdown = read_page_selection(note_relative_pages(page_records))["content"]
            atomic_write_json(sources_dir / f"{doc_name}.json", page_records, ensure_ascii=False)
        else:
            # Non-PDF, non-MD: use markitdown (docx, pptx, html, etc.).
            # Imported lazily: markitdown pulls in magika → onnxruntime (tens
            # of MB, slow to import) that only this branch needs. Keeping it
            # out of module import makes `import ultrakb` cheap and lets a
            # packaged build drop those deps when Office-format ingest isn't
            # required. markitdown appends warning filters on import but leaves
            # the CLI's front "ignore" filter in place, so no re-suppress here.
            from markitdown import MarkItDown

            mid = MarkItDown()
            result = mid.convert(str(prepared), keep_data_uris=True)
            markdown = result.text_content
            markdown = extract_base64_images(markdown, doc_name, images_dir)

        dest_md = sources_dir / f"{doc_name}.md"
        atomic_write_text(dest_md, markdown)

        return ConvertResult(
            raw_path=raw_dest,
            source_path=dest_md,
            file_hash=file_hash,
            doc_name=doc_name,
            source_identity=source_identity,
            processing=processing,
            pdf_path=internal_pdf,
            office_path=office_path,
            cnki_path=cnki_path,
            is_long_doc=processing is not None and processing.execution_mode == "segmented",
        )
