"""Conservative extraction checks, before source admission or model work.

These rules report observed text facts. They neither infer a document's cause of
damage nor rewrite the text. HTML needs a readable static body. PDFs without
usable text can retain their originals and images, without generating knowledge.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable

from ultrakb.file_digest import hash_file
from ultrakb.inputs import (
    CNKI_SOURCE_EXTENSIONS,
    OFFICE_SOURCE_EXTENSIONS,
    TEXT_SOURCE_EXTENSIONS,
    PreparedInput,
)
from ultrakb.pdf_recognition import PDF_RECOGNITION_POLICY, PdfRecognitionError, recognize_pdf

if TYPE_CHECKING:
    from ultrakb.workbooks.records import WorkbookSnapshot

TEXT_CHECK_POLICY = (
    "extracted-text-v2:cjk-runs12:3runs:60chars:ratio45:native-body-v2:" + PDF_RECOGNITION_POLICY
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
        if assessment.unavailable == "no_readable_text":
            super().__init__("未生成知识：没有可靠的原生文字；已有原件和原图仍可阅读。")
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
            hash_file(path),
            extraction,
            assess_text_parts(pdf_text_parts(path, allow_empty=allow_empty)),
        )
    except PdfRecognitionError as error:
        raise ImportTextRejected(
            ImportTextAssessment(hash_file(path), extraction, unavailable=error.reason)
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


def _native_body(frozen, extension: str) -> str:
    """Image captions/retention diagnostics alone cannot supply document knowledge."""
    if extension not in {".md", ".markdown", ".html", ".htm"}:
        return frozen.text
    from ultrakb.markdown_images import apply_image_edits, image_references

    body = "".join(
        frozen.text[start:end]
        for origin in frozen.origins
        for start, end in [origin.normalized_span]
        if origin.kind != "image_reference" and not (origin.html and origin.html.tag == "img")
    )
    return apply_image_edits(body, [(r.start, r.end, "") for r in image_references(body)])


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
        hash_file(path), "retained normalized text", assess_text_parts(parts)
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
    if hash_file(prepared.path) != prepared.digest:
        raise ValueError("Text preflight input failed its frozen digest check")
    if workbook is not None and workbook.digest != prepared.digest:
        raise ValueError("Text preflight workbook belongs to another original")
    extension = prepared.source.suffix.lower()
    cache_key = TEXT_CHECK_POLICY
    if extension in OFFICE_SOURCE_EXTENSIONS:
        import json

        from ultrakb.office.runtime import processing_identity

        cache_key += json.dumps(processing_identity(kb_dir), sort_keys=True)
    elif extension in CNKI_SOURCE_EXTENSIONS:
        import json

        from ultrakb.cnki.runtime import processing_identity as cnki_processing_identity

        cache_key += ":expected:" if cnki_identity is not None else ":retained-or-current:"
        cache_key += json.dumps(cnki_identity or cnki_processing_identity(), sort_keys=True)
    if cache_key in prepared.text_checks:
        return prepared.text_checks[cache_key]
    extraction = "extracted text"
    empty_html = False
    try:
        with tempfile.TemporaryDirectory(prefix="ultrakb-text-check-") as temporary:
            directory = Path(temporary)
            if extension == ".pdf":
                extraction = "PDF text layer"
                parts = pdf_text_parts(prepared.path)
            elif extension in CNKI_SOURCE_EXTENSIONS:
                from ultrakb.cnki.convert import prepare_cnki

                extraction = "CNKI internal PDF text layer"
                pdf, _ = prepare_cnki(
                    kb_dir, prepared, check_stop=check_stop, expected_identity=cnki_identity
                )
                parts = pdf_text_parts(pdf)
            elif extension in OFFICE_SOURCE_EXTENSIONS:
                from ultrakb.office.convert import convert_office

                extraction = "Office conversion/extraction result"
                pdf = directory / "converted.pdf"
                provenance = convert_office(kb_dir, prepared.path, pdf, check_stop=check_stop)
                # A derived slide PDF may be image-only while original notes
                # carry navigation text. The Office contract checks both domains.
                parts = pdf_text_parts(pdf, allow_empty=True)
                from ultrakb.office.slide_content import read_slides

                parts.extend(
                    (f"slide[{slide.ordinal}].notes", slide.notes)
                    for slide in read_slides(provenance)
                )
            elif extension in TEXT_SOURCE_EXTENSIONS:
                from ultrakb.remote_assets import resolve_resource_policy
                from ultrakb.text_formats import normalize_text_document

                frozen = normalize_text_document(
                    prepared,
                    "preflight",
                    directory / "wiki",
                    resource_policy=resolve_resource_policy(kb_dir, False)
                    if extension in {".html", ".htm"}
                    else None,
                )
                parts = [("body", _native_body(frozen, extension))]
                empty_html = extension in {".html", ".htm"} and not frozen.text.strip()
            elif extension in {".xlsx", ".xls"}:
                from ultrakb.workbooks.xls import read_xls
                from ultrakb.workbooks.xlsx import read_xlsx

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
        from ultrakb.locks import LockCancelled
        from ultrakb.mutation import RecoveryRequired

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
    assessment = ImportTextAssessment(
        prepared.digest,
        extraction,
        findings,
        # Workbooks own per-sheet empty, objects-only and parse-failure outcomes.
        unavailable="no_readable_text"
        if extension not in {".xlsx", ".xls"} and not any(text.strip() for _, text in parts)
        else None,
        empty_html=empty_html,
    )
    prepared.text_checks[cache_key] = assessment
    return assessment


def retainable_without_text(assessment: ImportTextAssessment) -> bool:
    """Successfully read originals with no usable text can retain their assets."""
    return not assessment.empty_html and (
        assessment.unavailable == "no_readable_text"
        or assessment.extraction == "PDF text layer"
        and bool(assessment.findings)
    )


def validate_text_preflight(
    prepared: PreparedInput, assessment: ImportTextAssessment, *, retain_only=False
) -> None:
    if assessment.digest != prepared.digest or assessment.policy != TEXT_CHECK_POLICY:
        raise ValueError("Text assessment does not match this frozen input and check policy")
    if hash_file(prepared.path) != assessment.digest:
        raise ValueError("Text preflight input changed after extraction")
    if retain_only and retainable_without_text(assessment):
        return
    if (
        assessment.findings
        or assessment.empty_html
        or assessment.unavailable == "no_readable_text"
        or assessment.extraction == "PDF text layer"
        and assessment.unavailable
    ):
        raise ImportTextRejected(assessment)


def rejection_result(prepared: PreparedInput, exc: ImportTextRejected):
    from ultrakb.ingest_result import IngestResult

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
