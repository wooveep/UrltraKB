"""Portable PDF snapshot plus original slide notes for the existing PDF parser."""

import base64
import hashlib
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pageindex.index.page_parts_policy import PAGE_PARTS_INDEX_POLICY

from openkb.locks import atomic_write_text
from openkb.office.records import Slide
from openkb.office.slide_content import NOTES_POLICY, attach_slides, read_slides
from openkb.source_records import Digest, Record


class SlidePackage(Record):
    format: Literal["openkb-slide-pdf-v1"] = "openkb-slide-pdf-v1"
    pdf: str
    pdf_digest: Digest
    slides: list[Slide]


def freeze_slide_package(pdf: Path, provenance: Path) -> Path | None:
    slides = read_slides(provenance)
    if not slides:
        return None
    data = pdf.read_bytes()
    package = SlidePackage(
        pdf=base64.b64encode(data).decode("ascii"),
        pdf_digest=hashlib.sha256(data).hexdigest(),
        slides=slides,
    )
    path = pdf.with_suffix(".okpi")
    atomic_write_text(path, package.model_dump_json())
    return path


@contextmanager
def materialize_slide_package(path: Path):
    package = SlidePackage.model_validate_json(path.read_text("utf-8"))
    data = base64.b64decode(package.pdf, validate=True)
    if hashlib.sha256(data).hexdigest() != package.pdf_digest:
        raise ValueError("Slide package PDF digest changed")
    with tempfile.TemporaryDirectory(prefix="openkb-slide-pdf-") as temporary:
        pdf = Path(temporary) / "snapshot.pdf"
        pdf.write_bytes(data)
        yield pdf, package.slides


class FrozenSlideParser:
    policy = f"{NOTES_POLICY}:{PAGE_PARTS_INDEX_POLICY}"

    def supported_extensions(self):
        return [".okpi"]

    def parse(self, file_path, **kwargs):
        from pageindex.parser.pdf import PdfParser
        from pageindex.tokens import count_tokens

        with materialize_slide_package(Path(file_path)) as (pdf, slides):
            parsed = PdfParser().parse(str(pdf), **kwargs)
        pages = attach_slides(
            [{"page": node.index, "content": node.content} for node in parsed.nodes], slides
        )
        for node, page in zip(parsed.nodes, pages):
            node.content = page["content"]
            node.tokens = count_tokens(node.content, kwargs.get("model"))
            node.metadata.update({key: page[key] for key in ("parts", "slide", "origin_locators")})
        parsed.metadata["notes_policy"] = NOTES_POLICY
        return parsed
