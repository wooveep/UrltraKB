"""Resolve observed original citations in document syntax, preserving literal examples."""

from __future__ import annotations

import hashlib
import html
import os
import re
import uuid
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.rules_inline import html_inline, image, link

from openkb.agent.answer_references import render_references, short_citation
from openkb.agent.evidence_markup import rewrite_inline
from openkb.agent.source_session import current_source_session
from openkb.evidence import Evidence, ParseStore


def markdown_links(text, replace):
    """Rewrite only parsed link destinations; retain code, escapes and block layout."""
    markdown = MarkdownIt("commonmark").enable(["table", "strikethrough"])
    environment = {}
    tokens = markdown.parse(text, environment)
    prose, edits = "", []

    def rule(original, is_image=False):
        def parse(state, silent):
            start, count = state.pos, len(state.tokens)
            if not original(state, silent):
                return False
            if silent or state.src is not prose:
                return True
            token = next(
                (
                    t
                    for t in state.tokens[count:]
                    if t.type == ("image" if is_image else "link_open")
                ),
                None,
            )
            if token is None:
                return True
            old = token.attrGet("src" if is_image else "href") or ""
            new = replace(old)
            if new == old:
                return True
            label_start = start + int(is_image)
            end = markdown.helpers.parseLinkLabel(state, label_start, True)
            if end < 0:
                return True
            position = end + 1
            if position < state.pos and prose[position] == "(":
                position += 1
                while prose[position : position + 1].isspace():
                    position += 1
                destination = markdown.helpers.parseLinkDestination(prose, position, state.pos)
                if destination.ok:
                    edits.append((position, destination.pos, new))
                    return True
            # Reference-style usage gets its own destination; the shared definition
            # is kept verbatim, including references used by literal code examples.
            edits.append((start, state.pos, prose[start : end + 1] + f"({new})"))
            return True

        return parse

    def html_rule(state, silent):
        start = state.pos
        if not html_inline(state, silent):
            return False
        if not silent and state.src is prose:
            from openkb.artifact_html import rewrite_html

            old = prose[start : state.pos]
            new = rewrite_html(old, replace)
            if old != new:
                edits.append((start, state.pos, new))
        return True

    markdown.inline.ruler.at("html_inline", html_rule)
    markdown.inline.ruler.at("link", rule(link))
    markdown.inline.ruler.at("image", rule(image, True))

    def inline(value):
        nonlocal prose, edits
        prose, edits = value, []
        markdown.inline.parse(value, markdown, environment, [])
        return edits

    return rewrite_inline(text, tokens, inline)


class ArtifactCitations:
    def __init__(self, kb_dir: Path):
        self.kb_dir = kb_dir
        self.markers: dict[str, str] = {}
        self.targets: dict[str, list[dict]] = {}
        self.used: list[dict] = []
        self.issues: list[str] = []
        self.checked_snapshots: set[str] = set()
        session = current_source_session(kb_dir)
        if session is None:
            return
        for marker, rows in session.observations.items():
            citations = {row["citation"] for row in rows}
            if len(citations) != 1:
                self.issues.append(f"conflicting_evidence: {marker}")
                continue
            citation = next(iter(citations))
            try:
                for row in rows:
                    reference = Evidence(**row["reference"])
                    expected = (
                        f"sources/snapshots/{reference.version_id}-{reference.parse_id}.md"
                        f"#block-{reference.block_id}"
                    )
                    if citation != f"[原文]({expected})" or marker != short_citation(citation):
                        raise ValueError("Evidence citation mismatch")
                    view = ParseStore(kb_dir).read(reference, max_chars=16000)
                    for image in row.get("images", []):
                        path = kb_dir / "wiki" / image["path"]
                        if (
                            image["asset"] not in view.assets
                            or not path.resolve().is_relative_to(kb_dir / "wiki/sources/images")
                            or hashlib.sha256(path.read_bytes()).hexdigest() != image["asset"]
                        ):
                            raise ValueError("Source image binding mismatch")
                        self.targets.setdefault(image["path"], []).append(
                            {**row, "asset": image["asset"]}
                        )
                self.markers[marker] = citation
                self.targets[expected] = rows
            except (ValueError, OSError, TypeError, KeyError):
                self.issues.append(f"invalid_evidence_binding: {marker}")

    def destination(self, file: Path, target: str) -> str:
        try:
            parts = urlsplit(target)
        except ValueError:
            self.issues.append(f"invalid_link: {target}")
            return target
        if parts.scheme or parts.netloc:
            if "sources" in Path(unquote(parts.path)).parts or parts.fragment.startswith("block-"):
                self.issues.append(f"unobserved_source: {target}")
            return target
        path = unquote(parts.path)
        if "sources" not in Path(path).parts and not parts.fragment.startswith("block-"):
            return target
        source = (
            (self.kb_dir / "wiki" / path) if path.startswith("sources/") else (file.parent / path)
        )
        try:
            canonical = source.resolve().relative_to(self.kb_dir / "wiki").as_posix()
        except ValueError:
            self.issues.append(f"unobserved_source: {target}")
            return target
        canonical += "#" + unquote(parts.fragment) if parts.fragment else ""
        rows = self.targets.get(canonical)
        if rows is None or parts.query:
            self.issues.append(f"unobserved_source: {target}")
            return target
        for row in rows:
            try:
                if not row.get("asset") and canonical not in self.checked_snapshots:
                    from openkb.navigation_tree import snapshot_markdown
                    from openkb.sources import SourceStore

                    reference = Evidence(**row["reference"])
                    expected = snapshot_markdown(
                        self.kb_dir,
                        SourceStore(self.kb_dir).version(reference.version_id),
                        ParseStore(self.kb_dir).load(reference.parse_id),
                    )
                    if source.read_text(encoding="utf-8") != expected:
                        self.issues.append(f"source_target_changed: {target}")
                        return target
                    self.checked_snapshots.add(canonical)
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
            except OSError:
                self.issues.append(f"source_target_unavailable: {target}")
                return target
            record = {"target": canonical, "reference": row["reference"], "target_sha256": digest}
            if row.get("asset"):
                record["asset"] = row["asset"]
            if record not in self.used:
                self.used.append(record)
        return quote(os.path.relpath(source, file.parent), safe="/.-") + (
            "#" + parts.fragment if parts.fragment else ""
        )

    def markdown(self, file: Path, text: str) -> str:
        return markdown_document(
            text, lambda value: self._markdown(file, value), lambda value: self.html(file, value)
        )

    def _markdown(self, file: Path, text: str) -> str:
        rendered, unresolved = render_references(text, self.markers)
        self.issues.extend(f"unobserved_citation: {marker}" for marker in unresolved)
        return markdown_links(rendered, lambda target: self.destination(file, target))

    def html(self, file: Path, text: str) -> str:
        from openkb.artifact_html import rewrite_html

        def marker(value):
            citation = self.markers.get(value)
            if citation is None:
                self.issues.append(f"unobserved_citation: {value}")
                return None
            target = self.destination(file, citation[len("[原文](") : -1])
            return f'<a href="{html.escape(target, quote=True)}">原文</a>'

        return rewrite_html(text, lambda target: self.destination(file, target), marker)


def markdown_document(text, markdown_rewrite, html_rewrite):
    """Let each grammar handle its own blocks; protect inline HTML literal elements."""
    parser = MarkdownIt("commonmark").enable(["table", "strikethrough"])
    tokens = parser.parse(text)
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    replacements, protected = [], {}

    def placeholder(value):
        key = "OPENKBHTML" + uuid.uuid4().hex
        protected[key] = value
        return key

    for token in tokens:
        if token.type == "html_block" and token.map:
            start, end = (offsets[n] for n in token.map)
            replacements.append(
                (start, end, placeholder(html_rewrite(text[start:end]).removesuffix("\n")) + "\n")
            )
    for start, end, value in reversed(replacements):
        text = text[:start] + value + text[end:]

    # Inline literal elements are HTML, even inside a Markdown paragraph. Use
    # the parser to find opening tags so escaped examples and code spans stay literal.
    tokens = parser.parse(text)

    def protect(value):
        edits = []

        def literal(state, silent):
            start = state.pos
            if not html_inline(state, silent):
                return False
            tag = re.match(
                r"<(code|pre|script|style|textarea|template|title)(?:\s|>)",
                state.src[start : state.pos],
                re.I,
            )
            if tag:
                closing = re.search(r"</" + tag[1] + r"\s*>", state.src[state.pos :], re.I)
                end = state.pos + closing.end() if closing else len(state.src)
                if not silent:
                    edits.append((start, end, placeholder(state.src[start:end])))
                state.pos = end
            return True

        parser.inline.ruler.at("html_inline", literal)
        parser.inline.parse(value, parser, {}, [])
        return edits

    text = rewrite_inline(text, tokens, protect)
    text = markdown_rewrite(text)
    for key, value in protected.items():
        text = text.replace(key, value)
    return text
