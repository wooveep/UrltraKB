"""CAJ object reconstruction and KDH decoding, derived from the pinned caj2pdf baseline.

Unlike the baseline's repair scan, reconstruction keeps only complete objects;
an incomplete duplicate is recoverable only when the same ID has a complete copy.
See assets/UPSTREAM.md for the baseline, licence and compatibility patch.
"""

import re
import struct
from collections import defaultdict
from pathlib import Path

import pymupdf


def detect_format(data: bytes) -> str:
    if data.startswith(b"%PDF-"):
        return "PDF"
    if data.startswith(b"CAJ\0"):
        return "CAJ"
    if data.startswith(b"KDH "):
        return "KDH"
    for signature, name in ((b"HN", "HN"), (b"\xc8", "C8"), (b"TEB", "TEB")):
        if data.startswith(signature):
            raise ValueError(f"Unsupported CNKI internal format: {name}")
    raise ValueError("Unsupported CNKI internal format: unknown signature")


def inspect_pdf(path: Path, *, declared_pages: int | None = None) -> int:
    with pymupdf.open(path) as pdf:
        if not pdf.is_pdf or pdf.needs_pass or pdf.page_count < 1 or pdf.is_repaired:
            raise ValueError("Invalid CNKI output: requires a complete, unencrypted PDF with pages")
        if declared_pages is not None and pdf.page_count != declared_pages:
            raise ValueError("CNKI PDF page count does not match the CAJ header")
        return pdf.page_count


def _caj_pdf(data: bytes) -> tuple[bytes, int, list[str]]:
    if len(data) < 24:
        raise ValueError("Truncated CAJ header")
    pages, pointer = struct.unpack_from("<II", data, 16)
    if not pages or pages > 100000 or not 24 <= pointer <= len(data) - 4:
        raise ValueError("Invalid CAJ header or PDF pointer")
    start = struct.unpack_from("<I", data, pointer)[0]
    stop = data.rfind(b"endobj") + 6
    if not pointer + 4 <= start < stop <= len(data):
        raise ValueError("Truncated CAJ PDF object data")
    raw = data[start:stop]
    heads = list(re.finditer(rb"(?:^|[\r\n])([1-9]\d*)\s+(\d+)\s+obj\b", raw))
    objects: dict[int, bytes] = {}
    incomplete: set[int] = set()
    for i, match in enumerate(heads):
        number = int(match[1])
        if int(match[2]) != 0 or number > 1000000:
            raise ValueError("Unsupported CAJ object generation or object number")
        following = heads[i + 1].start() if i + 1 < len(heads) else len(raw)
        end = raw.find(b"endobj", match.end())
        if end < 0 or following < end:
            incomplete.add(number)
            continue
        body = raw[match.end() : end].strip()
        stream = re.search(rb"stream\r?\n", body)
        if stream:
            length = re.search(rb"/Length\s+(\d+)\b", body[: stream.start()])
            if not length or not body[stream.end() + int(length[1]) :].lstrip(b"\r\n").startswith(
                b"endstream"
            ):
                incomplete.add(number)
                continue
        if number in objects and objects[number] != body:
            raise ValueError(f"Conflicting complete CAJ objects: {number}")
        objects[number] = body
    if not objects or incomplete - objects.keys():
        raise ValueError("CAJ contains incomplete objects without a complete duplicate")

    children: dict[int, list[int]] = defaultdict(list)
    page_objects = []
    for number, body in objects.items():
        if re.search(rb"/Type\s*/Pages?\b", body):
            parent_match = re.search(rb"/Parent\s+(\d+)\s+0\s+R\b", body)
            if parent_match:
                children[int(parent_match[1])].append(number)
            if re.search(rb"/Type\s*/Page\b", body):
                if not parent_match:
                    raise ValueError("CAJ page has no original parent")
                page_objects.append(number)
    if len(page_objects) != pages:
        raise ValueError("Complete CAJ page objects do not match the declared page count")
    # The format omits Pages objects. Reconstruct only these missing containers,
    # retaining original complete objects, references and physical source order.
    for parent, kids in children.items():
        if parent not in objects:
            if any(kid not in page_objects for kid in kids):
                raise ValueError("Unsupported missing CAJ nested page container")
            objects[parent] = (
                f"<</Type/Pages/Kids [{' '.join(f'{kid} 0 R' for kid in kids)}]/Count {len(kids)}>>"
            ).encode()
    roots = [
        number
        for number, body in objects.items()
        if re.search(rb"/Type\s*/Pages\b", body) and not re.search(rb"/Parent\b", body)
    ]
    if not roots:
        raise ValueError("CAJ has no recoverable page tree")
    root = max(objects) + 1
    catalog = root + 1
    objects[root] = (
        f"<</Type/Pages/Kids [{' '.join(f'{kid} 0 R' for kid in roots)}]/Count {pages}>>"
    ).encode()
    for number in roots:
        objects[number] = objects[number][:-2] + f"/Parent {root} 0 R>>".encode()
    objects[catalog] = f"<</Type/Catalog/Pages {root} 0 R>>".encode()
    result = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for number, body in objects.items():
        offsets[number] = len(result)
        result.extend(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref, size = len(result), max(objects) + 1
    result.extend(f"xref\n0 {size}\n0000000000 65535 f \n".encode())
    for number in range(1, size):
        result.extend(
            f"{offsets.get(number, 0):010} 00000 {'n' if number in objects else 'f'} \n".encode()
        )
    result.extend(
        f"trailer\n<</Size {size}/Root {catalog} 0 R>>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    diagnostics = [f"Recovered complete copies of {len(incomplete)} truncated duplicate objects."]
    return bytes(result), pages, diagnostics


def _verify_objects(path: Path, *, kdh_markers=False) -> tuple[bytes | None, list[str]]:
    pymupdf.TOOLS.mupdf_warnings(reset=True)
    with pymupdf.open(path) as pdf:
        for number in range(1, pdf.xref_length()):
            pdf.xref_object(number)
        for page in pdf:
            page.get_text()
        warnings = pymupdf.TOOLS.mupdf_warnings(reset=True).splitlines()
        metrics = [
            line
            for line in warnings
            if re.fullmatch(r"bogus font ascent/descent values \(-?\d+ / -?\d+\)", line)
        ]
        markers = [
            line
            for line in warnings
            if kdh_markers
            and re.fullmatch(r"line feed missing after stream begin marker \(\d+ \d+ R\)", line)
        ]
        unexpected = set(warnings) - set(metrics) - set(markers)
        if unexpected:
            raise ValueError(f"Invalid CNKI PDF objects: {'; '.join(sorted(unexpected))[:1000]}")
        rewritten = pdf.tobytes(no_new_id=True) if markers else None
    return rewritten, metrics


def convert(source: Path, output: Path) -> dict:
    data = source.read_bytes()
    kind = detect_format(data)
    declared_pages = None
    diagnostics: list[str] = []
    if kind == "CAJ":
        data, declared_pages, diagnostics = _caj_pdf(data)
    elif kind == "KDH":
        if len(data) <= 254:
            raise ValueError("Truncated KDH content")
        key = b"FZHMEI"
        data = bytes(value ^ key[i % len(key)] for i, value in enumerate(data[254:]))
        end = data.rfind(b"%%EOF")
        if not data.startswith(b"%PDF-") or end < 0:
            raise ValueError("Invalid KDH decoded PDF")
        data = data[: end + 5]
    output.write_bytes(data)
    pages = inspect_pdf(output, declared_pages=declared_pages)
    # Strictly parse every object and page in the isolated worker. A merely
    # tolerant open of a damaged intermediate file is never a success.
    rewritten, metrics = _verify_objects(output, kdh_markers=kind == "KDH")
    if rewritten is not None:
        output.write_bytes(rewritten)
        inspect_pdf(output, declared_pages=pages)
        _, metrics = _verify_objects(output)
        diagnostics.append("Canonicalized KDH PDF stream markers and revalidated every page.")
    diagnostics.extend(f"Native font metric diagnostic: {line}" for line in sorted(set(metrics)))
    return {
        "internal_format": kind,
        "pages": pages,
        "declared_pages": declared_pages,
        "diagnostics": diagnostics,
    }
