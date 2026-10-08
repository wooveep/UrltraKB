"""Conservative extraction checks, before source admission or model work.

These rules report observed text facts. They neither infer a document's cause of
damage nor rewrite the text. HTML needs a readable static body; original PDFs
without usable text are rejected before source admission.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable

from openkb.inputs import (
    CNKI_SOURCE_EXTENSIONS,
    OFFICE_SOURCE_EXTENSIONS,
    TEXT_SOURCE_EXTENSIONS,
    PreparedInput,
)
from openkb.pdf_recognition import PDF_RECOGNITION_POLICY, PdfRecognitionError, recognize_pdf
from openkb.state import HashRegistry

if TYPE_CHECKING:
    from openkb.workbooks.records import WorkbookSnapshot

TEXT_CHECK_POLICY = (
    "extracted-text-v1:cjk-runs12:3runs:60chars:ratio45:html-body-v1:" + PDF_RECOGNITION_POLICY
)
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_RUN = re.compile(r"([\u3400-\u4dbf\u4e00-\u9fff])\1{11,}")


@dataclass(frozen=True)
class TextFinding:
    reason: str
    location: str
    cjk_characters: int
    repeated_characters: int
    runs: int
    longest_run: int


@dataclass(frozen=True)
class ImportTextAssessment:
    digest: str
    extraction: str
    findings: tuple[TextFinding, ...] = ()
    unavailable: str | None = None
    policy: str = TEXT_CHECK_POLICY
    empty_html: bool = False


class ImportTextRejected(ValueError):
    def __init__(self, assessment: ImportTextAssessment):
        self.assessment = assessment
        if assessment.empty_html:
            super().__init__("HTML not imported: no readable body")
            return
        if assessment.extraction == "PDF text layer":
            reason = "无法可靠读取文本层"
            if assessment.findings:
                locations = "、".join(f.location for f in assessment.findings[:6])
                reason = f"文本层出现异常重复字符（{locations}，共 {len(assessment.findings)} 处）"
            super().__init__(
                f"PDF导入识别异常：{reason}。请检查原件，改用原生 Office 文档或重新导出的 PDF。"
            )
            return
        facts = "; ".join(
            f"{fact.reason} at {fact.location}: {fact.repeated_characters}/"
            f"{fact.cjk_characters} CJK characters in {fact.runs} repeated runs "
            f"(longest {fact.longest_run})"
            for fact in assessment.findings
        )
        super().__init__(f"Import text rejected ({assessment.extraction}): {facts}")


def assess_import_text(text: str, *, location: str = "body") -> tuple[TextFinding, ...]:
    """Reject several long CJK runs dominating a substantial text fragment.

    A single repeated word, digits, separators, short emphatic strings, and
    repeated normal sentences do not meet this rule.
    """
    runs = list(_RUN.finditer(text))
    count = len(_CJK.findall(text))
    repeated = sum(len(run[0]) for run in runs)
    if len(runs) < 3 or repeated < 60 or repeated < count * 0.45:
        return ()
    return (
        TextFinding(
            "repeated_cjk", location, count, repeated, len(runs), max(len(run[0]) for run in runs)
        ),
    )


def assess_text_parts(parts: Iterable[tuple[str, str]]) -> tuple[TextFinding, ...]:
    return tuple(
        fact for location, text in parts for fact in assess_import_text(text, location=location)
    )


def pdf_text_parts(path: Path, *, allow_empty: bool = False) -> list[tuple[str, str]]:
    return recognize_pdf(path, allow_empty=allow_empty)


def require_pdf_text(
    path: Path, *, extraction: str = "PDF text layer", allow_empty: bool = False
) -> None:
    try:
        assessment = ImportTextAssessment(
            HashRegistry.hash_file(path),
            extraction,
            assess_text_parts(pdf_text_parts(path, allow_empty=allow_empty)),
        )
    except PdfRecognitionError as error:
        raise ImportTextRejected(
            ImportTextAssessment(HashRegistry.hash_file(path), extraction, unavailable=error.reason)
        ) from error
    if assessment.findings:
        raise ImportTextRejected(assessment)


def _structured_text(value, location="body"):
    if isinstance(value, str):
        yield location, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _structured_text(item, f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _structured_text(item, f"{location}[{index}]")


def require_normalized_text(path: Path) -> None:
    """Guard retained inputs before recompilation, including legacy tree text."""
    import json

    if path.suffix.lower() == ".pdf":
        require_pdf_text(path)
        return
    text = path.read_text("utf-8")
    parts = (
        _structured_text(json.loads(text)) if path.suffix.lower() == ".json" else [("body", text)]
    )
    assessment = ImportTextAssessment(
        HashRegistry.hash_file(path), "retained normalized text", assess_text_parts(parts)
    )
    if assessment.findings:
        raise ImportTextRejected(assessment)


def preflight_import_text(
    kb_dir: Path,
    prepared: PreparedInput,
    *,
    check_stop: Callable[[], None] = lambda: None,
    workbook: WorkbookSnapshot | None = None,
    cnki_identity: dict | None = None,
) -> ImportTextAssessment:
    """Extract frozen bytes in temporary space; write no raw/wiki/catalog artifacts."""
    check_stop()
    if HashRegistry.hash_file(prepared.path) != prepared.digest:
        raise ValueError("Text preflight input failed its frozen digest check")
    if workbook is not None and workbook.digest != prepared.digest:
        raise ValueError("Text preflight workbook belongs to another original")
    extension = prepared.source.suffix.lower()
    cache_key = TEXT_CHECK_POLICY
    if extension in OFFICE_SOURCE_EXTENSIONS:
        import json

        from openkb.office.runtime import processing_identity

        cache_key += json.dumps(processing_identity(kb_dir), sort_keys=True)
    elif extension in CNKI_SOURCE_EXTENSIONS:
        import json

        from openkb.cnki.runtime import processing_identity as cnki_processing_identity

        cache_key += ":expected:" if cnki_identity is not None else ":retained-or-current:"
        cache_key += json.dumps(cnki_identity or cnki_processing_identity(), sort_keys=True)
    if cache_key in prepared.text_checks:
        return prepared.text_checks[cache_key]
    extraction = "extracted text"
    empty_html = False
    try:
        with tempfile.TemporaryDirectory(prefix="openkb-text-check-") as temporary:
            directory = Path(temporary)
            if extension == ".pdf":
                extraction = "PDF text layer"
                parts = pdf_text_parts(prepared.path)
            elif extension in CNKI_SOURCE_EXTENSIONS:
                from openkb.cnki.convert import prepare_cnki

                extraction = "CNKI internal PDF text layer"
                pdf, _ = prepare_cnki(
                    kb_dir, prepared, check_stop=check_stop, expected_identity=cnki_identity
                )
                parts = pdf_text_parts(pdf)
            elif extension in OFFICE_SOURCE_EXTENSIONS:
                from openkb.office.convert import convert_office

                extraction = "Office conversion/extraction result"
                pdf = directory / "converted.pdf"
                provenance = convert_office(kb_dir, prepared.path, pdf, check_stop=check_stop)
                # A derived slide PDF may be image-only while original notes
                # carry navigation text. The Office contract checks both domains.
                parts = pdf_text_parts(pdf, allow_empty=True)
                from openkb.office.slide_content import read_slides

                parts.extend(
                    (f"slide[{slide.ordinal}].notes", slide.notes)
                    for slide in read_slides(provenance)
                )
            elif extension in TEXT_SOURCE_EXTENSIONS:
                from openkb.remote_assets import resolve_resource_policy
                from openkb.text_formats import normalize_text_document

                frozen = normalize_text_document(
                    prepared,
                    "preflight",
                    directory / "wiki",
                    resource_policy=resolve_resource_policy(kb_dir, False)
                    if extension in {".html", ".htm"}
                    else None,
                )
                parts = [("body", frozen.text)]
                empty_html = extension in {".html", ".htm"} and not frozen.text.strip()
            elif extension in {".xlsx", ".xls"}:
                from openkb.workbooks.xls import read_xls
                from openkb.workbooks.xlsx import read_xlsx

                if workbook is not None and workbook.error:
                    raise ValueError(workbook.error)
                sheets = (
                    workbook.sheets
                    if workbook is not None
                    else (read_xlsx if extension == ".xlsx" else read_xls)(prepared.path)
                )
                parts = [
                    (f"worksheet[{sheet.name}]", "\n".join(cell.display for cell in sheet.cells))
                    for sheet in sheets
                ]
            elif extension == ".json":
                import json

                extraction = "retained structured text"
                parts = list(_structured_text(json.loads(prepared.path.read_text("utf-8"))))
            else:
                from markitdown import MarkItDown

                parts = [("body", MarkItDown().convert(str(prepared.path)).text_content)]
            findings = assess_text_parts(parts)
    except Exception as exc:
        # Existing format-specific failures still own unavailable extraction.
        # This is an attempted check, never an assertion of clean text.
        from openkb.locks import LockCancelled
        from openkb.mutation import RecoveryRequired

        if isinstance(exc, (LockCancelled, RecoveryRequired)):
            raise
        assessment = ImportTextAssessment(
            prepared.digest,
            extraction,
            unavailable=exc.reason if isinstance(exc, PdfRecognitionError) else type(exc).__name__,
        )
        prepared.text_checks[cache_key] = assessment
        return assessment
    check_stop()
    assessment = ImportTextAssessment(prepared.digest, extraction, findings, empty_html=empty_html)
    prepared.text_checks[cache_key] = assessment
    return assessment


def validate_text_preflight(prepared: PreparedInput, assessment: ImportTextAssessment) -> None:
    if assessment.digest != prepared.digest or assessment.policy != TEXT_CHECK_POLICY:
        raise ValueError("Text assessment does not match this frozen input and check policy")
    if HashRegistry.hash_file(prepared.path) != assessment.digest:
        raise ValueError("Text preflight input changed after extraction")
    if (
        assessment.findings
        or assessment.empty_html
        or assessment.extraction == "PDF text layer"
        and assessment.unavailable
    ):
        raise ImportTextRejected(assessment)


def rejection_result(prepared: PreparedInput, exc: ImportTextRejected):
    from openkb.ingest_result import IngestResult

    return IngestResult(
        str(prepared.identity),
        "skipped" if exc.assessment.empty_html else "rejected",
        (),
        quality=("empty_html_not_imported",)
        if exc.assessment.empty_html
        else ("import_text_rejected", "pdf_recognition_anomaly")
        if exc.assessment.extraction == "PDF text layer"
        else ("import_text_rejected",),
        unfinished=() if exc.assessment.empty_html else ("text_preflight",),
        input_version=prepared.digest,
        message=str(exc),
    )
