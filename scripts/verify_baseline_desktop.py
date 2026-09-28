"""Exercise the original import and chat pipelines with a local model fixture.

Run with the checkout's own pinned environment. No live model credentials are used.
"""

import argparse
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile


def create_inputs(root: Path) -> None:
    import pymupdf

    root.mkdir(parents=True, exist_ok=False)
    (root / "short.md").write_text(
        "# Baseline import\n\nShort Markdown is compiled directly into wiki knowledge.\n",
        encoding="utf-8",
    )
    with ZipFile(root / "office.docx", "w") as document:
        document.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
            'officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        document.writestr(
            "_rels/.rels",
            '<?xml version="1.0"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        )
        document.writestr(
            "word/document.xml",
            '<?xml version="1.0"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Original Office import: Word documents become Markdown "
            "before knowledge compilation.</w:t></w:r></w:p></w:body></w:document>",
        )
    for name, count in (("short.pdf", 2), ("long.pdf", 21)):
        with pymupdf.open() as document:
            for number in range(1, count + 1):
                page = document.new_page()
                page.insert_text(
                    (72, 72),
                    f"PortableProbe PDF page {number}\n"
                    "Original source content for the native baseline import.\n"
                    "Section facts are retained for long document retrieval.",
                )
            document.save(root / name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = args.output.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False)
    create_inputs(root / "inputs")
    checkout = Path(__file__).resolve().parents[1]
    subprocess.run(
        [
            sys.executable,
            str(checkout / "scripts/verify_desktop_model.py"),
            "--inputs",
            str(root / "inputs"),
            "--urls",
            "--output",
            str(root / "acceptance"),
        ],
        cwd=checkout,
        check=True,
        timeout=300,
    )


if __name__ == "__main__":
    main()
