"""Office inputs retain original bytes and use the common physical PDF readback."""

import os
import zipfile
from pathlib import Path

import pytest

pytest_plugins = ("test_pdf_readback",)


@pytest.fixture(autouse=True)
def isolated_global_settings(tmp_path_factory, monkeypatch):
    from openkb import config

    directory = tmp_path_factory.mktemp("global-settings")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", directory)
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", directory / "global.yaml")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_LOCK_PATH", directory / ".lock")


@pytest.fixture
def office_runtime(kb_dir):
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest

    value = os.environ.get("OPENKB_TEST_OFFICE_RUNTIME")
    if not value:
        pytest.skip("Real Office integration needs a prepared OPENKB_TEST_OFFICE_RUNTIME")
    root = Path(value).resolve()
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir),
            config={
                "office_runtime_path": str(root),
            },
        ),
    )
    return root


@pytest.fixture
def writer_document(tmp_path):
    path = tmp_path / "writer sample.docx"
    parts = {
        "[Content_Types].xml": (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'wordprocessingml.document.main+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" '
            'Target="word/document.xml"/>'
            "</Relationships>"
        ),
        "word/document.xml": (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:rPr><w:rFonts "
            'w:ascii="Source Code Pro" w:eastAsia="Source Han Sans CN"/></w:rPr>'
            "<w:t>First section hello 中文</w:t></w:r></w:p>"
            '<w:p><w:del w:id="1" w:author="Fixture">'
            "<w:r><w:delText>DELETED_MARKER</w:delText></w:r></w:del>"
            '<w:ins w:id="2" w:author="Fixture"><w:r><w:t>INSERTED_MARKER</w:t></w:r></w:ins></w:p>'
            "<w:p><w:r><w:rPr><w:vanish/></w:rPr><w:t>HIDDEN_MARKER</w:t></w:r></w:p>"
            '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'
            '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'
            '<w:p><w:r><w:rPr><w:rFonts w:ascii="Missing OpenKB Font"/></w:rPr>'
            "<w:t>Last section with fallback font.</w:t></w:r></w:p>"
            '<w:p><w:r><w:rPr><w:rFonts w:eastAsia="Missing CJK Font"/></w:rPr>'
            "<w:t>中文缺失字体</w:t></w:r></w:p>"
            '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>'
            "</w:body></w:document>"
        ),
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
        for name, content in parts.items():
            package.writestr(name, content)
    return path


def test_missing_office_runtime_keeps_raw_docx_and_pdf_still_imports(
    kb_dir, writer_document, physical_pdf, pdf_model
):
    from openkb.application.documents import import_document
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.documents import read_document_source

    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir),
            config={
                "office_runtime_path": str(kb_dir / "unavailable-office"),
            },
        ),
    )
    result = import_document(kb_dir, writer_document)
    assert result.status == "failed" and "Office runtime" in result.message
    saved = read_document_source(kb_dir, result.source_id)
    assert saved["knowledge_revision_id"] is None
    assert (kb_dir / saved["original_path"]).read_bytes() == writer_document.read_bytes()
    pdf = import_document(kb_dir, physical_pdf)
    assert pdf.status == "added", pdf.message


def test_private_serif_replacement_keeps_symbols_and_cjk_readable(
    kb_dir, writer_document, office_runtime, tmp_path
):
    import pymupdf

    from openkb.office.convert import convert_office

    with zipfile.ZipFile(writer_document) as package:
        parts = {name: package.read(name) for name in package.namelist()}
    parts["word/document.xml"] = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:r><w:rPr><w:rFonts w:ascii="思源宋体 CN" '
        'w:eastAsia="思源宋体 CN"/><w:b/></w:rPr>'
        "<w:t>80+ 40+ 100+ 20- 2× 50% 中文</w:t></w:r></w:p></w:body></w:document>"
    ).encode()
    with zipfile.ZipFile(writer_document, "w") as package:
        for name, body in parts.items():
            package.writestr(name, body)
    pdf_path = tmp_path / "symbols.pdf"
    convert_office(kb_dir, writer_document, pdf_path, check_stop=lambda: None)
    with pymupdf.open(pdf_path) as pdf:
        assert pdf.page_count == 1
        assert "80+ 40+ 100+ 20- 2× 50% 中文" in pdf[0].get_text()
        fonts = {
            span["font"]
            for block in pdf[0].get_text("dict")["blocks"]
            for line in block.get("lines", [])
            for span in line["spans"]
            if "+" in span["text"]
        }
        assert fonts == {"FrankRuhlHofshi-Bold"}


@pytest.mark.parametrize("limit", [10, 1])
def test_docx_freezes_final_visible_pages_and_exact_office_provenance(
    kb_dir, writer_document, office_runtime, pdf_model, limit
):
    import pymupdf

    from openkb.application.documents import import_document
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.documents import read_document_source

    apply_kb_config_patch(
        kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"pdf_short_max_pages": limit})
    )

    original = writer_document.read_bytes()
    result = import_document(kb_dir, writer_document)
    assert result.status == "added", result.message
    saved = read_document_source(kb_dir, result.source_id)
    assert (kb_dir / saved["original_path"]).read_bytes() == original
    assert saved["pages"] == 3
    assert "INSERTED_MARKER" in saved["content"]
    assert "DELETED_MARKER" not in saved["content"]
    assert "HIDDEN_MARKER" not in saved["content"]
    blank = read_document_source(kb_dir, result.source_id, pages="2")
    assert blank["units"][0]["content"].strip() == ""
    pdf_path = kb_dir / saved["internal_pdf_path"]
    assert pdf_path.read_bytes().startswith(b"%PDF-1.7")
    with pymupdf.open(pdf_path) as pdf:
        assert pdf.page_count == 3
        assert "中文" in pdf[0].get_text()
    office = saved["office"]
    assert office["version"] == "26.2.6.3"
    assert office["build_id"] == "8221e31b3ac356a1623c672912a3d2b492f7e3d1"
    assert office["filter"] == "writer_pdf_Export"
    assert office["runtime_fingerprint"] and office["pdf_digest"]
    assert any(item["requested"] == "Missing OpenKB Font" for item in office["font_substitutions"])
    assert any(item["requested"] == "Missing CJK Font" for item in office["font_substitutions"])
    assert office["font_observations"]


def test_corrupt_office_pdf_reference_cannot_mislabel_the_original_as_a_pdf(
    kb_dir, writer_document, office_runtime, pdf_model
):
    import json

    from openkb.application.documents import import_document
    from openkb.documents import read_document_source
    from openkb.locks import atomic_write_json

    result = import_document(kb_dir, writer_document)
    assert result.status == "added", result.message
    path = next((kb_dir / ".openkb/catalog/normalizations").glob("*.json"))
    record = json.loads(path.read_text())
    record["pdf_path"] = record["raw_path"]
    atomic_write_json(path, record)
    with pytest.raises(ValueError, match="Office|PDF"):
        read_document_source(kb_dir, result.source_id)


@pytest.mark.parametrize("duplicate_deleted", [False, True])
def test_deleted_substring_cannot_claim_the_visible_runs_font(
    kb_dir, writer_document, office_runtime, pdf_model, duplicate_deleted
):
    from openkb.application.documents import import_document
    from openkb.documents import read_document_source

    with zipfile.ZipFile(writer_document) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts["word/document.xml"] = (
        parts["word/document.xml"]
        .replace(b"First section hello", b"prefix foobar suffix")
        .replace(
            b"<w:r><w:delText>DELETED_MARKER",
            b'<w:r><w:rPr><w:rFonts w:ascii="DeletedOnlyFont"/></w:rPr><w:delText>foo',
        )
    )
    if duplicate_deleted:
        parts["word/document.xml"] = parts["word/document.xml"].replace(
            b"</w:del>",
            (
                '</w:del><w:del w:id="3" w:author="Fixture"><w:r><w:delText>'
                "prefix foobar suffix 中文</w:delText></w:r></w:del>"
            ).encode(),
        )
    with zipfile.ZipFile(writer_document, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    result = import_document(kb_dir, writer_document)
    assert result.status == "added", result.message
    saved = read_document_source(kb_dir, result.source_id)
    assert "prefix foobar suffix" in saved["content"]
    assert not any(
        item["requested"] == "DeletedOnlyFont" for item in saved["office"]["font_substitutions"]
    )


def test_docx_recompile_and_history_read_the_retained_pdf_without_office(
    kb_dir, writer_document, office_runtime, pdf_model
):
    import asyncio

    from openkb.application.documents import import_document
    from openkb.application.recompilation import recompile_document, select_recompilation
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.documents import read_document_source

    imported = import_document(kb_dir, writer_document)
    assert imported.status == "added", imported.message
    before = read_document_source(kb_dir, imported.source_id)
    frozen = (kb_dir / before["internal_pdf_path"]).read_bytes()
    writer_document.unlink()
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir),
            config={
                "office_runtime_path": str(kb_dir / "gone"),
                "pdf_short_max_pages": 0,
            },
        ),
    )
    selected = select_recompilation(kb_dir, imported.source_id, confirmation=True)
    result = asyncio.run(recompile_document(kb_dir, imported.source_id, version=selected.version))
    assert result.status == "compiled", result.message
    after = read_document_source(kb_dir, imported.source_id)
    assert after["office"] == before["office"]
    assert after["content"] == before["content"]
    assert after["processing"] == before["processing"]
    assert (kb_dir / after["internal_pdf_path"]).read_bytes() == frozen
    historical = read_document_source(
        kb_dir,
        imported.source_id,
        source_revision_id=before["source_revision_id"],
    )
    assert historical["office"] == before["office"]


def test_docx_api_and_cli_expose_original_pdf_and_conversion_diagnostics(
    kb_dir, writer_document, office_runtime, pdf_model
):
    import json

    from click.testing import CliRunner
    from fastapi.testclient import TestClient

    from openkb.api import create_app
    from openkb.cli import cli
    from openkb.config import register_kb_alias

    register_kb_alias("office", kb_dir)
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/add",
            data={"kb": "office", "stream": "false"},
            files={"files": (writer_document.name, writer_document.read_bytes())},
        )
        assert response.status_code == 200, response.text
        result = response.json()["files"][0]
        assert result["status"] == "added", result
        read = client.post(
            "/api/v1/document/source",
            json={"kb": "office", "hash": result["source_id"], "pages": "2"},
        )
    assert read.status_code == 200, read.text
    body = read.json()
    assert body["office"]["pages"] == 3
    assert (kb_dir / body["internal_pdf_path"]).is_file()
    output = CliRunner().invoke(cli, ["--kb-dir", str(kb_dir), "source", result["source_id"]])
    assert output.exit_code == 0, output.output
    saved = json.loads(output.output)
    assert saved["office"] == body["office"]
    assert (kb_dir / saved["original_path"]).read_bytes() == writer_document.read_bytes()


def test_broken_office_update_retains_previous_body_and_new_original(
    kb_dir, writer_document, office_runtime, pdf_model
):
    from openkb.application.documents import import_document
    from openkb.documents import read_document_source

    first = import_document(kb_dir, writer_document)
    assert first.status == "added", first.message
    before = read_document_source(kb_dir, first.source_id)
    writer_document.write_bytes(b"PK\x03\x04truncated office file")
    second = import_document(kb_dir, writer_document)
    assert second.status == "failed", second.message
    current = read_document_source(kb_dir, first.source_id)
    assert current["content"] == before["content"]
    assert current["office"] == before["office"]
    assert current["target_source_revision_id"] != current["source_revision_id"]
    failed = read_document_source(
        kb_dir, first.source_id, source_revision_id=second.source_revision_id
    )
    assert (kb_dir / failed["original_path"]).read_bytes() == writer_document.read_bytes()
    assert failed.get("internal_pdf_path") is None


@pytest.fixture(params=["conversion", "probe"])
def hanging_office(tmp_path, monkeypatch, request):
    """Replace only soffice; use the real supervisor and private UNO bridge."""
    import json
    import subprocess
    import sys
    from types import SimpleNamespace

    if sys.platform != "linux":
        pytest.skip("POSIX tree fixture; the Windows Job is exercised by #101")
    pids = tmp_path / "owned-pids.json"
    executable = tmp_path / "hanging-office"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import subprocess,sys,os,json,time\n"
        "from pathlib import Path\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        f"Path({str(pids)!r}).write_text(json.dumps([os.getpid(),child.pid]))\n"
        f"Path({str(pids.with_suffix('.task'))!r}).write_text(os.environ['TMPDIR'])\n"
        "time.sleep(60)\n"
    )
    executable.chmod(0o700)
    launch = subprocess.Popen
    stage = request.param

    class OfficeBoundary(launch):
        def __init__(self, command, *args, **kwargs):
            if any(str(value).endswith("/supervisor.py") for value in command):
                path = Path(command[-1])
                data = json.loads(path.read_text())
                if stage == "probe" and "command" in data and "--version" in data["command"]:
                    data["command"][0] = str(executable)
                elif stage == "conversion" and "soffice" in data:
                    data["soffice"] = str(executable)
                path.write_text(json.dumps(data))
            super().__init__(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", OfficeBoundary)
    return SimpleNamespace(pids=pids, executable=executable, stage=stage)


@pytest.mark.parametrize("stop", ["timeout", "cancel"])
def test_stopping_office_reaps_its_tree_and_keeps_original(
    kb_dir, writer_document, office_runtime, hanging_office, stop
):
    import json

    from openkb.application.documents import import_document
    from openkb.application.execution import ExecutionContext
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.application.sources import source_inventory
    from openkb.documents import read_document_source
    from openkb.locks import LockCancelled

    original = writer_document.read_bytes()
    if stop == "timeout":
        apply_kb_config_patch(
            kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"office_timeout_seconds": 1})
        )
        result = import_document(kb_dir, writer_document)
        assert result.status == "failed" and "timed out" in result.message
    else:
        context = ExecutionContext(cancelled=hanging_office.pids.exists)
        with pytest.raises(LockCancelled):
            import_document(kb_dir, writer_document, context=context)
    pids = json.loads(hanging_office.pids.read_text())
    assert all(not Path(f"/proc/{pid}").exists() for pid in pids)
    documents = source_inventory(kb_dir)
    if stop == "cancel":
        # Text preflight is before admission. Cancellation still reaps owned
        # Office processes and leaves the user's original bytes intact.
        assert documents == [] and writer_document.read_bytes() == original
        return
    saved = read_document_source(kb_dir, documents[0]["source_id"])
    assert saved["knowledge_revision_id"] is None
    assert (kb_dir / saved["original_path"]).read_bytes() == writer_document.read_bytes()


def test_application_exit_reaps_probe_or_conversion_and_its_private_profile(
    kb_dir, writer_document, office_runtime, hanging_office, tmp_path
):
    import json
    import subprocess
    import sys
    import time

    owned = tmp_path / "private-inputs"
    owned.mkdir()
    code = """
import json, subprocess, sys
from pathlib import Path
from openkb import config
from openkb.application.documents import import_document
from openkb.inputs import preparation_directory
kb, source, executable, stage, owned = sys.argv[1:]
config.GLOBAL_CONFIG_DIR = Path(owned) / "settings"
config.GLOBAL_CONFIG_PATH = config.GLOBAL_CONFIG_DIR / "global.yaml"
config.GLOBAL_CONFIG_LOCK_PATH = config.GLOBAL_CONFIG_DIR / ".lock"
original = subprocess.Popen
def boundary(command, *args, **kwargs):
    if any(str(value).endswith('/supervisor.py') for value in command):
        path = Path(command[-1]); data = json.loads(path.read_text())
        if stage == 'probe' and 'command' in data and '--version' in data['command']:
            data['command'][0] = executable
        elif stage == 'conversion' and 'soffice' in data:
            data['soffice'] = executable
        path.write_text(json.dumps(data))
    return original(command, *args, **kwargs)
subprocess.Popen = boundary
with preparation_directory(Path(owned)):
    import_document(Path(kb), Path(source))
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            code,
            str(kb_dir),
            str(writer_document),
            str(hanging_office.executable),
            hanging_office.stage,
            str(owned),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        while not hanging_office.pids.with_suffix(".task").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert hanging_office.pids.is_file()
        pids = json.loads(hanging_office.pids.read_text())
        profile = Path(hanging_office.pids.with_suffix(".task").read_text())
        process.kill()
        process.wait(timeout=5)
        deadline = time.monotonic() + 8
        while (
            any(Path(f"/proc/{pid}").exists() for pid in pids) or profile.exists()
        ) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert all(not Path(f"/proc/{pid}").exists() for pid in pids)
        assert not profile.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
