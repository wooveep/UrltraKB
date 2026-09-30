"""Freeze one isolated Office conversion; all subsequent reads use these bytes."""

import sys
import tempfile
import time
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape

import pymupdf

from openkb.locks import atomic_write_json
from openkb.mutation import _copy_file_atomic
from openkb.office.font_observation import observe_fonts
from openkb.office.inventory import digest
from openkb.office.policy import LOAD_OPTIONS, PDF_OPTIONS, office_environment
from openkb.office.processes import run_supervised
from openkb.office.records import OfficeConversion, WorkerResult
from openkb.office.runtime import processing_identity, require_runtime


def convert_office(
    kb_dir: Path,
    source: Path,
    pdf: Path,
    *,
    check_stop: Callable[[], None],
    expected_identity: dict | None = None,
) -> Path:
    identity = processing_identity(kb_dir)
    if expected_identity is not None and identity != expected_identity:
        raise ValueError("Office processing policy changed after the import was planned")
    check_stop()
    root, manifest, actual_probe = require_runtime(
        kb_dir, check_stop=check_stop, task_root=source.parent
    )
    if processing_identity(kb_dir) != identity:
        raise ValueError("Office runtime changed during its validation")
    started = time.monotonic()
    # The caller's owned input root also participates in runtime-worker recovery.
    with tempfile.TemporaryDirectory(prefix="openkb-office-task-", dir=source.parent) as temporary:
        directory = Path(temporary)
        environment = office_environment(directory)
        if sys.platform == "linux":
            fontconfig = directory / "fonts.conf"
            fontconfig.write_text(
                '<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">'
                "<fontconfig><dir>" + escape(str(root.resolve() / "share/fonts")) + "</dir>"
                "<cachedir>" + escape(str(directory / "font-cache")) + "</cachedir></fontconfig>",
                encoding="utf-8",
            )
            environment.update(
                FONTCONFIG_FILE=str(fontconfig), XDG_CACHE_HOME=str(directory / "cache")
            )
        request = {
            "input": str(source.resolve()),
            "soffice": str(root.resolve() / manifest.soffice),
            "timeout": identity["timeout"],
            "load_options": LOAD_OPTIONS,
            "pdf_options": PDF_OPTIONS,
        }
        run_supervised(
            root, manifest.python, manifest.launcher, directory, request, environment, check_stop
        )
        details = WorkerResult.model_validate_json(
            (directory / "result.json").read_text()
        ).model_dump(mode="json")
        output = directory / "output.pdf"
        if not output.read_bytes().startswith(b"%PDF-1.7"):
            raise ValueError("Office did not generate the required PDF 1.7")
        with pymupdf.open(output) as document:
            if document.page_count < 1 or document.needs_pass:
                raise ValueError("Office generated an unreadable PDF")
            spans = [
                span
                for page in document
                for block in page.get_text("dict")["blocks"]
                for line in block.get("lines", [])
                for span in line["spans"]
            ]
            details.update(
                pages=document.page_count,
                pdf_digest=digest(output),
                pdf_fonts=sorted({span["font"] for span in spans}),
                **observe_fonts(details.pop("requested_fonts"), spans),
            )
        check_stop()
        unresolved = sum(item["status"] != "matched" for item in details["font_observations"])
        if unresolved:
            details["diagnostics"].append(
                f"{unresolved} font runs could not be matched uniquely to visible PDF text; "
                "unmatched runs may include deleted text. No replacement is inferred for them."
            )
        _copy_file_atomic(output, pdf)
        provenance = pdf.with_suffix(".office.json")
        record = OfficeConversion.model_validate(
            {
                **details,
                "version": manifest.version,
                "build_id": manifest.build_id,
                "python_version": manifest.python_version,
                "runtime_fingerprint": manifest.fingerprint,
                "processing_identity": identity,
                "probe": actual_probe,
                "archive": manifest.archive.model_dump(mode="json"),
                "source_archive": manifest.source.model_dump(mode="json"),
                "fonts": manifest.fonts,
                "licenses": manifest.licenses,
                "elapsed_seconds": time.monotonic() - started,
                "input_digest": digest(source),
            }
        )
        atomic_write_json(provenance, record.model_dump(mode="json"), ensure_ascii=False)
        return provenance
