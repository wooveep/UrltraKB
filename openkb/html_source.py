"""Traceable HTML-to-Markdown using the existing pinned HTML/Markdown libraries."""

import re
from html import escape, unescape
from pathlib import Path
from urllib.parse import unquote

from bs4 import BeautifulSoup, Tag
from markdownify import MarkdownConverter

from openkb.html_images import html_image_references, html_link_targets
from openkb.source_records import TextDecoding
from openkb.text_encoding import decode_text
from openkb.text_measurement import measure_markdown
from openkb.text_source import (
    FrozenText,
    HtmlLocation,
    ResourcePolicy,
    SourceResource,
    TextOrigin,
    freeze_markdown,
)

HTML_POLICY = "html-bs4-4.14.3-html.parser-markdownify-1.2.2-block-origins-v1"
_BLOCKS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "pre", "ul", "ol", "table", "blockquote", "hr"}
_OMITTED = {"script", "style", "head", "template", "noscript"}
_CONTAINERS = {
    "[document]",
    "html",
    "body",
    "div",
    "section",
    "article",
    "main",
    "header",
    "footer",
    "nav",
    "aside",
}


def _distribute_block_links(soup):
    """Markdown links cannot span paragraphs; keep a flow link on each text run."""
    layout_tags = (
        _BLOCKS | _CONTAINERS | {"li", "tr", "td", "th", "thead", "tbody", "tfoot", "caption"}
    )
    for link in list(soup.find_all("a", href=True)):
        if link.find(list(layout_tags)) is None:
            continue
        attributes = {key: link[key] for key in ("href", "title") if key in link.attrs}

        def distribute(container):
            run = []

            def flush():
                if any(isinstance(node, Tag) or str(node).strip() for node in run):
                    anchor = soup.new_tag("a", attrs=attributes)
                    run[0].insert_before(anchor)
                    for node in run:
                        anchor.append(node.extract())
                run.clear()

            for child in list(container.children):
                if isinstance(child, Tag) and (
                    child.name in layout_tags or child.find(list(layout_tags))
                ):
                    flush()
                    distribute(child)
                else:
                    run.append(child)
            flush()

        distribute(link)
        link.unwrap()


def _location(tag) -> HtmlLocation:
    names = []
    current = tag
    while current and current.name != "[document]":
        ordinal = 1 + len(current.find_previous_siblings(current.name))
        names.append(f"{current.name}[{ordinal}]")
        current = current.parent
    return HtmlLocation(tag=tag.name, path="/" + "/".join(reversed(names)))


def _rewritten_position(markup: FrozenText, position: int) -> int:
    for origin in markup.origins:
        a, b = origin.original_span
        left, right = origin.normalized_span
        if position == a:
            return left
        if position == b:
            return right
        if a < position < b and origin.kind == "identity":
            return left + position - a
    raise ValueError("HTML block boundary crosses an image rewrite")


def freeze_html(
    prepared,
    doc_name: str,
    wiki: Path,
    decoding: TextDecoding | None = None,
    *,
    resource_policy: ResourcePolicy | None = None,
) -> FrozenText:
    from openkb.text_formats import text_normalization_policy

    original, encoding, diagnostics = decode_text(prepared.path.read_bytes(), decoding)
    policy = resource_policy or ResourcePolicy()
    from openkb.remote_assets import retain_remote_images

    references = html_image_references(original)
    edits, failures = retain_remote_images(references, wiki, doc_name, policy)
    downloaded = {(a, b) for a, b, _ in edits}
    for start, end, address in html_link_targets(original):
        if address != unescape(original[start:end]):
            edits.append((start, end, escape(address, quote=True)))
    for reference in references:
        raw_reference = unescape(original[reference.start : reference.end])
        local = prepared.images.get(reference.source)
        if (
            (reference.start, reference.end) not in downloaded
            and reference.source != raw_reference
            and (local is None or local.path is None)
        ):
            edits.append((reference.start, reference.end, escape(reference.source, quote=True)))
    markup = freeze_markdown(
        prepared,
        doc_name,
        wiki,
        original=original,
        additional_image_edits=edits,
        references=references,
    )
    resources = []
    for reference in references:
        rewrite = next(
            (
                origin
                for origin in markup.origins
                if origin.kind == "image_reference"
                and origin.original_span == (reference.start, reference.end)
            ),
            None,
        )
        path = (
            "sources/" + unquote(markup.text[slice(*rewrite.normalized_span)]) if rewrite else None
        )
        if path not in markup.assets:
            path = None
        kind = (
            "data"
            if reference.source.startswith("data:")
            else "local"
            if reference.local_path is not None
            else "remote"
        )
        resources.append(
            SourceResource(
                reference=reference.source.split(",", 1)[0]
                if kind == "data"
                else unescape(original[reference.start : reference.end]),
                resolved_reference=reference.source if kind != "data" else None,
                original_span=(reference.start, reference.end),
                kind=kind,
                status="retained"
                if path
                else "failed"
                if reference.source in failures
                else "not_requested"
                if kind == "remote"
                else "missing",
                path=path,
                digest=markup.assets.get(path) if path else None,
                message=failures.get(reference.source),
            )
        )
    soup = BeautifulSoup(original, "html.parser")
    offsets = [0, *(match.end() for match in re.finditer("\n", original))]
    offset = int(original.startswith("\ufeff"))
    boundaries: dict[int, HtmlLocation | None] = {offset: None}
    for tag in soup.find_all(list(_BLOCKS)):
        # An enclosing link/emphasis/list is conversion context. Keep it in
        # one fragment instead of cutting away the opening markup at a child.
        if any(parent.name not in _CONTAINERS for parent in tag.parents):
            continue
        if tag.sourceline is not None and tag.sourcepos is not None:
            boundaries[offsets[tag.sourceline - 1] + tag.sourcepos] = _location(tag)
    positions = sorted(boundaries)
    chunks, origins = [], []
    if offset:
        origins.append(TextOrigin(normalized_span=(0, 0), original_span=(0, offset), kind="bom"))
    converter = MarkdownConverter(
        heading_style="ATX",
        bullets="*",
        escape_misc=True,
        keep_inline_images_in=list({"a", *(tag.parent.name for tag in soup.find_all("img"))}),
    )
    cursor = 0
    for start, end in zip(positions, [*positions[1:], len(original)], strict=True):
        fragment = markup.text[
            _rewritten_position(markup, start) : _rewritten_position(markup, end)
        ]
        content = BeautifulSoup(fragment, "html.parser")
        for tag in list(content.find_all(list(_OMITTED))):
            tag.decompose()
        _distribute_block_links(content)
        text = converter.convert_soup(content).strip()
        text = text + "\n\n" if text else ""
        chunks.append(text)
        origins.append(
            TextOrigin(
                normalized_span=(cursor, cursor + len(text)),
                original_span=(start, end),
                kind="html",
                html=boundaries[start],
            )
        )
        cursor += len(text)
    text = "".join(chunks)
    return FrozenText(
        text=text,
        original_characters=len(original),
        original_digest=prepared.digest,
        origins=tuple(origins),
        assets=markup.assets,
        tokens=measure_markdown(text),
        normalization_policy=text_normalization_policy("html"),
        encoding=encoding,
        diagnostics=(*diagnostics, *markup.diagnostics, *failures.values()),
        resources=tuple(resources),
        resource_policy=policy,
    )
