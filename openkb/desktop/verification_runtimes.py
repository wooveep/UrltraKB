"""Exercise bundled document helpers from the extracted, frozen installation."""

import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile


def verify_document_runtimes(kb, root):
    if not getattr(sys, "frozen", False):
        return []
    checks = []
    if sys.platform in {"linux", "win32"}:
        from openkb.cfb_helper.runtime import helper_path

        result = subprocess.run(
            [str(helper_path()), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.stdout.strip() == "openkb-cfb 1.0.0 cfb 0.15.0"
        checks.append("bundled CFB helper integrity and native execution")
    supported_office = (
        sys.platform in {"linux", "win32"} and platform.machine().lower() in {"x86_64", "amd64"}
    ) or (sys.platform == "darwin" and platform.machine().lower() == "arm64")
    if not supported_office:
        return checks

    return checks + verify_office_conversions(kb, root)


def verify_office_conversions(kb, root):
    """Use the real shared converter, including slide ordinals and speaker notes."""

    import pymupdf

    from openkb.locks import kb_ingest_lock
    from openkb.office.convert import convert_office

    source = root / "bundled-office.docx"
    with ZipFile(source, "w") as document:
        document.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
            'officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        document.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        )
        document.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Bundled Office acceptance.</w:t></w:r></w:p></w:body>"
            "</w:document>",
        )
    pdf = root / "bundled-office.pdf"
    with kb_ingest_lock(kb / ".openkb"):
        convert_office(kb, source, pdf, check_stop=lambda: None)
    with pymupdf.open(pdf) as document:
        assert document.page_count == 1
        assert "Bundled Office acceptance." in document[0].get_text()
    checks = ["bundled Office/Python/UNO converts DOCX to readable PDF"]
    fixtures = Path(__file__).parent / "assets/office-check"
    for name in ("writer.doc", "slides.ppt", "slides.pptx"):
        source = root / name
        shutil.copyfile(fixtures / name, source)
        pdf = root / (name + ".pdf")
        with kb_ingest_lock(kb / ".openkb"):
            receipt = convert_office(kb, source, pdf, check_stop=lambda: None)
        with pymupdf.open(pdf) as document:
            assert document.page_count == 3
            marker = "First section hello" if name.endswith(".doc") else "Alpha visible slide"
            assert marker in document[0].get_text()
            if name.startswith("slides."):
                assert "HIDDEN_SLIDE_BODY" in document[1].get_text()
                details = json.loads(receipt.read_text("utf-8"))
                assert [slide["hidden"] for slide in details["slides"]] == [False, True, False]
                assert "rotate key every 90 days" in details["slides"][0]["notes"]
                assert details["slides"][2]["notes"] == "Notes-only page evidence."
        checks.append(
            f"bundled Office converts {source.suffix.upper()} with physical pages and notes"
        )
    return checks
