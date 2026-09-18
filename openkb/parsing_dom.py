"""Local HTML and XML retain native tree locations instead of conversion lines."""

import json
import re
from urllib.parse import unquote, urlsplit

from lxml import etree, html

from openkb.docx_containers import decode_text
from openkb.evidence import BlockDraft
from openkb.parsing_failures import DocumentContentError
from openkb.processing import processing_checkpoint


def _document(data):
    declared = re.search(rb"<meta[^>]+charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", data[:8192], re.I)
    try:
        text = data.decode(declared[1].decode("ascii")) if declared else decode_text(data)
        return html.document_fromstring(
            text.encode("utf-8"), parser=html.HTMLParser(encoding="utf-8")
        )
    except (LookupError, UnicodeError, etree.ParserError) as exc:
        raise DocumentContentError("html_content_unparsed") from exc


def html_images(text, directory):
    root = directory.resolve()
    try:
        document = _document(text)
    except DocumentContentError:
        return {}  # Intake still saves malformed originals; parsing owns the quality report.
    result = {}
    for reference in document.xpath("//img/@src"):
        url = urlsplit(reference)
        if url.scheme or url.netloc or not url.path:
            continue
        path = (root / unquote(url.path)).resolve()
        if path.is_relative_to(root):
            result[reference] = path
    return result


def parse_html(path, source, store):
    document = _document(path.read_bytes())
    tree = document.getroottree()
    bodies = document.xpath("//body")
    root = bodies[0] if bodies else document
    selection = "body" if bodies else "document"
    blocks, quality, headings = [], [], []

    def location(element):
        value = {
            "kind": "html",
            "dom_path": tree.getpath(element),
            "selection": selection,
            "headings": [row[1] for row in headings],
        }
        if element.get("id"):
            value["dom_id"] = element.get("id")
        return value

    def walk(element):
        nonlocal headings
        processing_checkpoint()
        tag = element.tag
        if not isinstance(tag, str) or tag in {"script", "style", "head"}:
            return
        if tag == "img":
            reference, alt = element.get("src", ""), element.get("alt", "")
            digest = source.assets.get(reference)
            if digest:
                store.asset(digest)
            else:
                quality.append({"status": "needs_review", "reason": "missing_asset:" + reference})
            blocks.append(
                BlockDraft(
                    f"![{alt}]({'asset:' + digest if digest else reference})",
                    "image",
                    location(element),
                    (digest,) if digest else (),
                )
            )
            return
        heading = tag in {"h1", "h2", "h3", "h4", "h5", "h6"}
        terminal = heading or tag in {"p", "li", "pre", "table", "figcaption", "blockquote"}
        if terminal:
            text = "".join(element.itertext())
            if heading:
                level = int(tag[1])
                headings = [row for row in headings if row[0] < level] + [(level, text)]
            loc = location(element)
            if heading:
                loc["heading_level"] = int(tag[1])
            context = ""
            if tag == "table":
                # Serialize row and cell boundaries, including merged-cell attributes.
                rows = [
                    [
                        {"text": "".join(cell.itertext()), "attributes": dict(cell.attrib)}
                        for cell in row.xpath("./th|./td")
                    ]
                    for row in element.xpath(".//tr")
                ]
                text = json.dumps(rows, ensure_ascii=False)
                context = "HTML table; each outer entry is a row, each inner entry a cell."
            kind = (
                "heading"
                if heading
                else {"pre": "code", "table": "table", "li": "list"}.get(tag, "paragraph")
            )
            if text.strip():
                blocks.append(BlockDraft(text, kind, loc, context=context))
            for child in element.xpath(".//img"):
                walk(child)
            return
        if element.text and element.text.strip():
            blocks.append(BlockDraft(element.text, "paragraph", location(element)))
        for child in element:
            walk(child)
            if child.tail and child.tail.strip():
                loc = location(child)
                loc["text_role"] = "tail"
                blocks.append(BlockDraft(child.tail, "paragraph", loc))

    walk(root)
    if not blocks:
        quality.append({"status": "needs_review", "reason": "empty_content"})
    return blocks, quality


def parse_xml(path, store):
    document = etree.parse(str(path), etree.XMLParser(resolve_entities=False, no_network=True))
    if document.docinfo.doctype:
        raise DocumentContentError("XML DTDs are not expanded")
    blocks = []

    def emit(element, text, role):
        name = etree.QName(element).localname if isinstance(element.tag, str) else "#comment"
        loc = {
            "kind": "xml",
            "element_path": document.getpath(element),
            "namespaces": {key or "": value for key, value in element.nsmap.items()},
            "attributes": dict(element.attrib),
            "text_role": role,
        }
        # Explicit document vocabularies only; generic configuration stays generic.
        root_name = etree.QName(document.getroot()).localname
        is_title = root_name in {"article", "book", "chapter", "section"} and name == "title"
        kind = "heading" if is_title and role == "text" else "paragraph"
        if not text.strip() and element.attrib and role == "text":
            text = (
                etree.tostring(element, encoding="unicode", with_tail=False).split(">", 1)[0] + ">"
            )
            kind = "metadata"
        if name == "#comment":
            kind = "metadata"
        if kind == "heading":
            loc["heading_level"] = min(9, max(1, len(list(element.iterancestors()))))
        blocks.append(BlockDraft(text, kind, loc))

    def walk(element):
        processing_checkpoint()
        if not isinstance(element.tag, str):
            if element.text:
                emit(element, element.text, "text")
            return
        # Empty elements still carry structural/attribute metadata and a stable position.
        emit(element, element.text or "", "text")
        for child in element:
            walk(child)
            if child.tail:
                emit(child, child.tail, "tail")

    walk(document.getroot())
    return blocks, []
