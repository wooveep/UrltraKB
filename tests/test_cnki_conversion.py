"""CNKI conversion uses file signatures and publishes only verified PDFs."""

from pathlib import Path

import pymupdf
import pytest


@pytest.fixture
def pdf_bytes():
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "Original CNKI content.")
        return pdf.tobytes(deflate=True)


def test_kdh_conversion_keeps_original_and_validates_final_pdf(tmp_path, pdf_bytes):
    from openkb.cnki.convert import convert_cnki
    from openkb.cnki.records import CNKIConversion
    from openkb.state import HashRegistry

    key = b"FZHMEI"
    data = b"KDH 2.00".ljust(254, b"\0") + bytes(
        byte ^ key[i % len(key)] for i, byte in enumerate(pdf_bytes)
    )
    source = tmp_path / "中文 空格.kdh"
    source.write_bytes(data)
    output = tmp_path / "managed" / "internal.pdf"
    record_path = convert_cnki(source, output)
    record = CNKIConversion.model_validate_json(record_path.read_text())
    assert source.read_bytes() == data
    assert record.internal_format == "KDH"
    assert record.input_digest == HashRegistry.hash_file(source)
    assert record.pdf_digest == HashRegistry.hash_file(output)
    assert record.pages == 1
    with pymupdf.open(output) as pdf:
        assert not pdf.needs_pass and not pdf.is_repaired
        assert "Original CNKI content." in pdf[0].get_text()


def caj_bytes(pdf_bytes, *, fragment=False):
    import re
    import struct

    body = pdf_bytes[pdf_bytes.index(b"1 0 obj") : pdf_bytes.index(b"xref")]
    body = re.sub(rb"(?:1|2) 0 obj\b.*?endobj\s*", b"", body, flags=re.S)
    if fragment:
        body = b"5 0 obj<</Type/Font/Encoding/Win\n" + body
    header = bytearray(64)
    header[:4] = b"CAJ\0"
    struct.pack_into("<III", header, 16, 1, 24, 64)
    return bytes(header) + body


@pytest.mark.parametrize("fragment", [False, True])
def test_caj_reconstructs_missing_containers_and_complete_duplicate_objects(
    tmp_path, pdf_bytes, fragment
):
    from openkb.cnki.convert import convert_cnki
    from openkb.cnki.records import CNKIConversion

    source = tmp_path / "source.CAJ"
    source.write_bytes(caj_bytes(pdf_bytes, fragment=fragment))
    output = tmp_path / "internal.pdf"
    record = CNKIConversion.model_validate_json(convert_cnki(source, output).read_text())
    assert record.internal_format == "CAJ" and record.pages == record.declared_pages == 1
    with pymupdf.open(output) as pdf:
        assert not pdf.is_repaired
        assert "Original CNKI content." in pdf[0].get_text()


@pytest.mark.parametrize(
    "data", [b"HN\0\0", b"\xc8\0\0\0", b"TEB\0", b"unknown", b"CAJ\0", b"KDH "]
)
def test_unsupported_or_truncated_content_does_not_publish_artifacts(tmp_path, data):
    from openkb.cnki.convert import convert_cnki

    source, output = tmp_path / "fake.caj", tmp_path / "internal.pdf"
    source.write_bytes(data)
    with pytest.raises(ValueError, match="Unsupported|Truncated|failed"):
        convert_cnki(source, output)
    assert not output.exists() and not output.with_suffix(".cnki.json").exists()
    assert source.read_bytes() == data


@pytest.mark.parametrize("failure", ["nonzero", "count", "timeout", "cancel"])
def test_failed_native_attempt_cleans_process_and_never_publishes_pdf(
    tmp_path, pdf_bytes, monkeypatch, failure
):
    import subprocess
    import sys

    from openkb.cnki.convert import convert_cnki

    source, output = tmp_path / "中文 路径.caj", tmp_path / "managed.pdf"
    source.write_bytes(pdf_bytes)
    processes = []
    attempts = []
    popen = subprocess.Popen
    code = (
        "import json, shutil, sys, time; "
        "shutil.copyfile(sys.argv[1], sys.argv[2]); "
        "open(sys.argv[3], 'w').write(json.dumps(dict(internal_format='PDF', "
        f"pages={2 if failure == 'count' else 1}, declared_pages=None, diagnostics=[]))); "
        + (
            "time.sleep(30)"
            if failure in {"timeout", "cancel"}
            else "sys.exit(1)"
            if failure == "nonzero"
            else "sys.exit(0)"
        )
    )

    def worker(command, **kwargs):
        attempts.append(Path(kwargs["cwd"]))
        process = popen([sys.executable, "-c", code, *command[-3:]], **kwargs)
        processes.append(process)
        return process

    def check_stop():
        if failure == "cancel" and processes:
            raise InterruptedError("fixture cancelled")

    monkeypatch.setattr(subprocess, "Popen", worker)
    message = {
        "nonzero": "conversion failed",
        "count": "changed during",
        "timeout": "timed out",
        "cancel": "fixture cancelled",
    }[failure]
    with pytest.raises((ValueError, TimeoutError, InterruptedError), match=message):
        convert_cnki(
            source, output, check_stop=check_stop, timeout=0.1 if failure == "timeout" else 3
        )
    assert processes[0].poll() is not None
    assert all(not path.exists() for path in attempts)
    assert not output.exists() and not output.with_suffix(".cnki.json").exists()
    assert source.read_bytes() == pdf_bytes


def test_parallel_conversion_uses_private_directories_and_identical_pdfs(tmp_path, pdf_bytes):
    from concurrent.futures import ThreadPoolExecutor

    from openkb.cnki.convert import convert_cnki

    source = tmp_path / "same source.caj"
    source.write_bytes(pdf_bytes)
    outputs = [tmp_path / name / "internal.pdf" for name in ("one", "two")]
    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(executor.map(lambda path: convert_cnki(source, path), outputs))
    assert outputs[0].read_bytes() == outputs[1].read_bytes() == pdf_bytes
    assert receipts[0] != receipts[1] and all(path.is_file() for path in receipts)


def test_password_pdf_and_wrong_caj_page_count_fail(tmp_path, pdf_bytes):
    import struct

    from openkb.cnki.convert import convert_cnki

    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
        password = pdf.tobytes(
            encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="reader"
        )
    caj = bytearray(caj_bytes(pdf_bytes))
    struct.pack_into("<I", caj, 16, 2)
    for data in (password, caj):
        source, output = tmp_path / "source.caj", tmp_path / "internal.pdf"
        source.write_bytes(data)
        with pytest.raises(ValueError, match="failed"):
            convert_cnki(source, output)
        assert not output.exists()


def test_kdh_rewrites_nonstandard_stream_markers_before_publication(tmp_path, pdf_bytes):
    from openkb.cnki.convert import convert_cnki

    key = b"FZHMEI"
    data = pdf_bytes.replace(b"stream\n", b"stream\r")
    source, output = tmp_path / "old-format.kdh", tmp_path / "internal.pdf"
    source.write_bytes(
        b"KDH 2.00".ljust(254, b"\0")
        + bytes(byte ^ key[i % len(key)] for i, byte in enumerate(data))
    )
    convert_cnki(source, output)
    pymupdf.TOOLS.mupdf_warnings(reset=True)
    with pymupdf.open(output) as pdf:
        assert "Original CNKI content." in pdf[0].get_text()
        assert not pdf.is_repaired
    assert not pymupdf.TOOLS.mupdf_warnings(reset=True)


def test_frozen_launcher_dispatches_worker_before_application_imports(monkeypatch):
    import runpy
    import sys

    calls = []
    monkeypatch.setattr("openkb.cnki.worker.main", lambda argv: calls.append(argv) or 0)
    monkeypatch.setattr(sys, "argv", ["UrltraKB", "--cnki-worker", "original", "pdf", "receipt"])
    with pytest.raises(SystemExit) as result:
        runpy.run_path(
            str(Path(__file__).parents[1] / "packaging/desktop/launcher.py"), run_name="__main__"
        )
    assert result.value.code == 0
    assert calls == [["original", "pdf", "receipt"]]
