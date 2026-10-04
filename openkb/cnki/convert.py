"""Terminate failed attempts and retain only validated conversion artifacts."""

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

from openkb.cnki.engine import detect_format, inspect_pdf
from openkb.cnki.records import CNKIConversion, CNKIWorkerResult
from openkb.cnki.runtime import identity_key, processing_identity, require_runtime
from openkb.locks import atomic_write_bytes, atomic_write_text
from openkb.state import HashRegistry


def convert_cnki(
    source: Path,
    output: Path,
    *,
    check_stop: Callable[[], None] = lambda: None,
    timeout: float = 120,
    expected_identity: dict | None = None,
) -> Path:
    check_stop()
    if source.resolve() == output.resolve():
        raise ValueError("CNKI internal PDF must be distinct from the original")
    if timeout <= 0:
        raise ValueError("CNKI conversion timeout must be positive")
    with source.open("rb") as stream:
        kind = detect_format(stream.read(8))
    identity = processing_identity()
    require_runtime(identity)
    if expected_identity is not None and expected_identity != identity:
        raise ValueError("CNKI processing identity changed during preparation")
    digest = HashRegistry.hash_file(source)
    with tempfile.TemporaryDirectory(prefix="openkb-cnki-") as temporary:
        task = Path(temporary)
        pdf, response = task / "converted.pdf", task / "result.json"
        command = [sys.executable]
        command += (
            ["--cnki-worker"] if getattr(sys, "frozen", False) else ["-m", "openkb.cnki.worker"]
        )
        command += [str(source.resolve()), str(pdf), str(response)]
        started = time.monotonic()
        with (task / "diagnostics.log").open("w+b") as diagnostics:
            process = subprocess.Popen(command, cwd=task, stdout=diagnostics, stderr=diagnostics)
            try:
                while process.poll() is None:
                    check_stop()
                    if time.monotonic() - started >= timeout:
                        raise TimeoutError("CNKI conversion timed out")
                    time.sleep(0.05)
                check_stop()
                if process.returncode != 0:
                    diagnostics.seek(0)
                    message = diagnostics.read(4096).decode("utf-8", errors="replace")
                    if response.is_file():
                        try:
                            failure = json.loads(response.read_text("utf-8"))
                            if isinstance(failure, dict) and isinstance(failure.get("error"), str):
                                message = failure["error"] or message
                        except (OSError, ValueError):
                            pass
                    raise ValueError(f"CNKI conversion failed: {message.strip()}")
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
        if not pdf.is_file() or not response.is_file():
            raise ValueError("CNKI conversion did not return its PDF and receipt")
        result = CNKIWorkerResult.model_validate_json(response.read_text("utf-8"))
        if result.internal_format != kind:
            raise ValueError("CNKI conversion receipt does not match the original format")
        pages = inspect_pdf(pdf, declared_pages=result.declared_pages)
        if HashRegistry.hash_file(source) != digest or pages != result.pages:
            raise ValueError("CNKI original or conversion changed during the attempt")
        record = CNKIConversion(
            input_digest=digest,
            processing_identity=identity,
            pdf_digest=HashRegistry.hash_file(pdf),
            **result.model_dump(),
        )
        check_stop()
        record_path = output.with_suffix(".cnki.json")
        atomic_write_bytes(output, pdf.read_bytes())
        atomic_write_text(record_path, record.model_dump_json())
        return record_path


def _retained_cnki(kb_dir, prepared, expected_identity):
    from openkb.normalization import NormalizedInput, read_normalization
    from openkb.source_catalog import read_record

    for path in sorted((kb_dir / ".openkb/catalog/normalizations").glob("*.json")):
        saved = read_record(kb_dir, "normalizations", path.stem, NormalizedInput)
        if saved.cnki_path and saved.files.get(saved.raw_path) == prepared.digest:
            directory, saved = read_normalization(kb_dir, saved.normalization_id)
            receipt = directory / saved.cnki_path
            record = CNKIConversion.model_validate_json(receipt.read_text("utf-8"))
            if expected_identity is None or record.processing_identity == expected_identity:
                return directory / saved.pdf_path, receipt, record.processing_identity
    return None


def prepare_cnki(kb_dir: Path, prepared, *, check_stop=lambda: None, expected_identity=None):
    check_stop()
    retained = _retained_cnki(kb_dir, prepared, expected_identity)
    identity = retained[2] if retained else processing_identity()
    if expected_identity is not None and identity != expected_identity:
        raise ValueError("CNKI processing identity changed during preparation")
    key = identity_key(identity)
    if key not in prepared.conversions:
        if retained:
            prepared.conversions[key] = retained[:2]
        else:
            temporary = tempfile.TemporaryDirectory(prefix="openkb-cnki-prepared-")
            pdf = Path(prepared.conversion_tasks.enter_context(temporary)) / "internal.pdf"
            try:
                receipt = convert_cnki(
                    prepared.path, pdf, check_stop=check_stop, expected_identity=identity
                )
            except Exception as exc:
                temporary.cleanup()
                prepared.conversions[key] = exc
                raise
            prepared.conversions[key] = (pdf, receipt)
    if isinstance(prepared.conversions[key], Exception):
        raise prepared.conversions[key]
    pdf, path = prepared.conversions[key]
    record = CNKIConversion.model_validate_json(path.read_text("utf-8"))
    if (
        record.input_digest != prepared.digest
        or record.processing_identity != identity
        or HashRegistry.hash_file(prepared.path) != prepared.digest
        or HashRegistry.hash_file(pdf) != record.pdf_digest
    ):
        raise ValueError("Prepared CNKI artifacts changed or belong to another input")
    inspect_pdf(pdf, declared_pages=record.pages)
    check_stop()
    return pdf, path
