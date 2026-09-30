"""Retain one sparse worksheet as a common frozen Markdown input."""

import json

from openkb.converter import ConvertResult
from openkb.locks import atomic_write_json, atomic_write_text
from openkb.mutation import _copy_file_atomic
from openkb.processing_policy import classify_markdown_tokens
from openkb.text_measurement import measure_markdown
from openkb.text_source import FrozenText, TextOrigin
from openkb.workbooks.records import WORKBOOK_POLICY


def convert_sheet(prepared, sheet, doc_name, directory):
    if sheet.error:
        raise ValueError(f"Worksheet {sheet.name}: {sheet.error}")
    text = ""
    origins = []

    def append(value, *, cell=None):
        nonlocal text
        start = len(text)
        text += value
        origins.append(
            TextOrigin(
                normalized_span=(start, len(text)),
                original_span=(0, 0),
                coordinate="sheet_cell" if cell else "unicode_codepoint",
                kind="sheet_cell" if cell else "generated",
                sheet_cell={
                    "sheet_key": sheet.key,
                    "sheet_name": sheet.name,
                    "cell": cell.model_dump(mode="json"),
                }
                if cell
                else None,
            )
        )

    append(f"Worksheet: {json.dumps(sheet.name, ensure_ascii=False)} ({sheet.state})\n\n")
    for cell in sheet.cells:
        append(f"{cell.coordinate} · {cell.data_type} · ")
        append(json.dumps(cell.display, ensure_ascii=False), cell=cell)
        if cell.formula_status != "not_formula":
            append(f" · formula {cell.formula_status}; cached {cell.cache_status}: ")
            if cell.cache_status == "available":
                append(json.dumps(cell.cached, ensure_ascii=False), cell=cell)
        append("\n\n")
    diagnostics = list(sheet.diagnostics)
    if any(cell.cache_status == "missing" for cell in sheet.cells):
        diagnostics.append("Formula cache missing; formulas were not calculated.")
    frozen = FrozenText(
        text=text,
        original_characters=0,
        original_digest=prepared.digest,
        origins=tuple(origins),
        assets={},
        tokens=measure_markdown(text),
        normalization_policy=WORKBOOK_POLICY,
        diagnostics=tuple(diagnostics),
        sheet=sheet.model_copy(update={"cells": ()}),
    )
    raw = directory / "raw" / f"{doc_name}{prepared.source.suffix.lower()}"
    _copy_file_atomic(prepared.path, raw)
    source = directory / "wiki/sources" / f"{doc_name}.md"
    atomic_write_text(source, text)
    atomic_write_json(source.with_suffix(".content.json"), frozen.model_dump(mode="json"))
    processing = classify_markdown_tokens(frozen.tokens)
    return ConvertResult(
        raw_path=raw,
        source_path=source,
        doc_name=doc_name,
        is_long_doc=processing.execution_mode == "segmented",
        processing=processing,
    )
