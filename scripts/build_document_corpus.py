"""Build deterministic, synthetic issue-13 documents and input-derived ground truth.

No model or service is called. Run in the repository's main development runtime.
Every family includes facts absent from its table of contents. Output must be a
new directory, and explicit elapsed-time/disk bounds apply to the entire build.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pymupdf

WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
FACTS = (
    "Version 7.1 requires database schema 12 before starting the service.",
    "Set --timeout=42; authentication failures must never be retried.",
    "After backup verification, run ukb migrate --strict --batch-size=17; "
    "skip this command on a read-only replica.",
)


def sha256(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


class Corpus:
    def __init__(self, root, seconds, max_bytes):
        self.started = time.monotonic()
        self.root, self.seconds, self.max_bytes = root, seconds, max_bytes
        self.records = []
        root.mkdir(parents=True, exist_ok=False)

    def check(self):
        if time.monotonic() - self.started > self.seconds:
            raise TimeoutError("Corpus generation time bound exhausted")
        size = sum(p.stat().st_size for p in self.root.iterdir() if p.is_file())
        if size > self.max_bytes:
            raise RuntimeError("Corpus generation disk bound exhausted")

    def record(self, name, family, scale, facts, **details):
        self.check()
        path = self.root / name
        self.records.append(
            {
                "file": name,
                "family": family,
                "scale": scale,
                "sha256": sha256(path),
                "bytes": path.stat().st_size,
                "facts": facts,
                **details,
            }
        )

    def docx(self, target):
        parts, facts = [], []
        total = 0
        paragraph = 0

        def text(value, heading=False):
            nonlocal total, paragraph
            paragraph += 1
            total += len(value)
            style = '<w:pPr><w:pStyle w:val="Heading1"/></w:pPr>' if heading else ""
            parts.append(
                f'<w:p>{style}<w:r><w:t xml:space="preserve">{escape(value)}</w:t></w:r></w:p>'
            )

        for index, fact in enumerate(FACTS):
            text(f"Section {index + 1}: operational procedures", heading=True)
            if index < 2:
                text(fact)
            facts.append({"text": fact, "section": index + 1})
            # Main-body table text counts toward the exact requested body size.
            values = ("Parameter", "Default", "--max-attempts", "3; except authentication failures")
            total += sum(map(len, values))
            parts.append(
                "<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>"
                + "".join(
                    "<w:tr>"
                    + ("<w:trPr><w:tblHeader/></w:trPr>" if row == 0 else "")
                    + "".join(
                        f"<w:tc><w:p><w:r><w:t>{escape(v)}</w:t></w:r></w:p></w:tc>"
                        for v in values[row * 2 : row * 2 + 2]
                    )
                    + "</w:tr>"
                    for row in range(2)
                )
                + "</w:tbl>"
            )
            boundary = (
                target // 2
                if index == 0
                else target
                - len("Section 3: operational procedures")
                - sum(map(len, values))
                - len(FACTS[2])
                if index == 1
                else target - len(fact)
            )
            while total < boundary:
                self.check()
                filler = (
                    "Procedure context: preserve the verified backup "
                    "and inspect the recorded result. "
                    "Keep ordered prerequisites, parameters and exceptions with their commands. "
                    "检查执行顺序并保留原始证据。\n"
                ) * 8
                text(filler[: min(len(filler), boundary - total)])
            if index == 2:
                text(fact)
        assert total == target
        name = f"docx-{target}.docx"
        content_types = (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'wordprocessingml.document.main+xml"/>'
            '<Override PartName="/word/styles.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'wordprocessingml.styles+xml"/>'
            "</Types>"
        )
        relationship = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
        files = {
            "[Content_Types].xml": content_types,
            "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{relationship}officeDocument" '
            'Target="word/document.xml"/></Relationships>',
            "word/_rels/document.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="styles" Type="{relationship}styles" '
            'Target="styles.xml"/></Relationships>',
            "word/styles.xml": f'<w:styles xmlns:w="{WORD}">'
            '<w:style w:type="paragraph" w:styleId="Heading1">'
            '<w:name w:val="heading 1"/></w:style></w:styles>',
            "word/document.xml": f'<w:document xmlns:w="{WORD}"><w:body>'
            f"{''.join(parts)}<w:sectPr/></w:body></w:document>",
        }
        with ZipFile(self.root / name, "w", compression=ZIP_DEFLATED) as archive:
            for path, contents in files.items():
                item = ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
                item.compress_type = ZIP_DEFLATED
                archive.writestr(item, contents.encode())
        # Count text from the actual archived body, not the helper's counter.
        import xml.etree.ElementTree as ET

        with ZipFile(self.root / name) as archive:
            body = ET.fromstring(archive.read("word/document.xml"))
            assert sum(len(node.text or "") for node in body.iter(f"{{{WORD}}}t")) == target
        self.record(
            name,
            "docx",
            target,
            facts,
            body_chars=target,
            layout="headings, paragraphs, tables, commands",
        )

    def native_pdf(self, pages):
        name = f"native-{pages}.pdf"
        positions = (1, pages // 2, pages)
        facts = []
        with pymupdf.open() as pdf:
            for number in range(1, pages + 1):
                self.check()
                width, height = (595, 842) if number % 3 else (612, 792)
                page = pdf.new_page(width=width, height=height)
                page.insert_text((32, 35), f"Section {number}: operational reference", fontsize=16)
                columns = 2 if number % 2 == 0 else 1
                for column in range(columns):
                    left = 32 + column * (width / 2)
                    rectangle = pymupdf.Rect(
                        left, 70, min(left + width / columns - 50, width - 24), height - 100
                    )
                    body = (
                        f"Section {number}, column {column + 1}. "
                        "Read steps in this column in order.\n"
                        "First inspect the backup. Then validate the transaction result.\n"
                        "Record the source version and retain the associated evidence.\n"
                    ) * 3
                    assert page.insert_textbox(rectangle, body, fontsize=10 + (number % 3)) >= 0
                if number in positions:
                    fact = FACTS[positions.index(number)]
                    assert (
                        page.insert_textbox(
                            pymupdf.Rect(32, height - 90, width - 32, height - 30),
                            fact,
                            fontsize=11,
                        )
                        >= 0
                    )
                    facts.append({"text": fact, "page": number})
                page.insert_text(
                    (32, height - 15), f"Reference manual | physical page {number}", fontsize=8
                )
            toc = "none" if pages == 100 else "valid" if pages == 500 else "incorrect"
            if toc != "none":
                pdf.set_toc([[1, f"Chapter {i}", i if toc == "valid" else 1] for i in positions])
            (self.root / name).write_bytes(pdf.tobytes(garbage=4, deflate=True, no_new_id=True))
        with pymupdf.open(self.root / name) as pdf:
            assert pdf.page_count == pages
            for fact in facts:
                assert fact["text"].split()[0] in pdf[fact["page"] - 1].get_text()
        self.record(
            name,
            "native_pdf",
            pages,
            facts,
            physical_pages=pages,
            toc=toc,
            layout="one/two columns, varied page/font sizes, running headers/footers",
        )

    def mixed_pdf(self, pages):
        name = f"mixed-{pages}.pdf"
        kinds = (
            "native",
            "scan",
            "bad_layer",
            "blank",
            "illustration",
            "rotated_scan",
            "near_match",
            "table",
        )
        facts, decisions = [], []
        with pymupdf.open() as pdf:
            for number in range(1, pages + 1):
                self.check()
                kind = kinds[(number - 1) % len(kinds)]
                page = pdf.new_page(width=432, height=576)
                decisions.append(
                    {
                        "page": number,
                        "kind": kind,
                        "requires_ocr_or_review": kind
                        in {"scan", "bad_layer", "blank", "illustration", "rotated_scan"},
                    }
                )
                if kind == "blank":
                    continue
                if kind == "illustration":
                    page.draw_rect(
                        pymupdf.Rect(80, 80, 350, 450), color=(0, 0, 1), fill=(0.8, 0.9, 1)
                    )
                    continue
                fact = f"Page {number}: " + FACTS[(number - 1) % 3]
                if kind == "near_match":
                    fact = f"Page {number}: Set --timeout=43; all other steps remain unchanged."
                facts.append({"text": fact, "page": number, "kind": kind})
                with pymupdf.open() as visual:
                    visual_page = visual.new_page(width=432, height=576)
                    visual_page.insert_text((25, 35), f"Operational section {number}", fontsize=15)
                    assert (
                        visual_page.insert_textbox(
                            pymupdf.Rect(25, 65, 405, 300), fact, fontsize=13
                        )
                        >= 0
                    )
                    if kind in {"scan", "bad_layer", "rotated_scan"}:
                        image = visual_page.get_pixmap(dpi=100).tobytes("png")
                        page.insert_image(page.rect, stream=image)
                        if kind == "bad_layer":
                            page.insert_text(
                                (25, 350), "Incorrect hidden text: timeout 9999.", render_mode=3
                            )
                        if kind == "rotated_scan":
                            page.set_rotation(90)
                    else:
                        page.show_pdf_page(page.rect, visual, 0)
                if kind == "table" or (number > 1 and kind == "native"):
                    # Identical column names intentionally span consecutive physical pages.
                    for y in (330, 365, 400):
                        page.draw_line((25, y), (400, y))
                    for x in (25, 210, 400):
                        page.draw_line((x, 330), (x, 400))
                    for x, y, text in (
                        (35, 352, "Parameter"),
                        (220, 352, "Value"),
                        (35, 387, f"parameter-{number}"),
                        (220, 387, f"{number} seconds"),
                    ):
                        page.insert_text((x, y), text, fontsize=11)
            (self.root / name).write_bytes(pdf.tobytes(garbage=4, deflate=True, no_new_id=True))
        with pymupdf.open(self.root / name) as pdf:
            assert pdf.page_count == pages
        self.record(name, "mixed_pdf", pages, facts, physical_pages=pages, pages=decisions)

    def build(self):
        for size in (100_000, 500_000, 1_000_000):
            self.docx(size)
        for pages in (100, 500, 1000):
            self.native_pdf(pages)
        for pages in (8, 32, 128, 500):
            self.mixed_pdf(pages)
        manifest = {
            "schema": 1,
            "generator_sha256": sha256(Path(__file__)),
            "documents": self.records,
            "synthetic": True,
        }
        (self.root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        )
        self.check()
        return {
            "documents": len(self.records),
            "bytes": sum(row["bytes"] for row in self.records),
            "elapsed_seconds": time.monotonic() - self.started,
            "manifest_sha256": sha256(self.root / "manifest.json"),
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("--max-bytes", type=int, required=True)
    args = parser.parse_args()
    if args.seconds <= 0 or args.max_bytes <= 0:
        parser.error("Supply positive time and disk limits")
    print(json.dumps(Corpus(args.output.resolve(), args.seconds, args.max_bytes).build()))
