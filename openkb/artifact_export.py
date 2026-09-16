"""Portable artifact packages with only their explicit, actually used source evidence."""

from __future__ import annotations

import html
import json
import posixpath
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from openkb.artifact_citations import markdown_document, markdown_links
from openkb.artifact_html import rewrite_html
from openkb.artifact_quality import read_quality
from openkb.evidence import Evidence, ParseStore
from openkb.file_state import contained_paths
from openkb.sources import SourceStore, content_id


def portable_files(kb_dir: Path, artifact: Path, files: tuple[str, ...]) -> dict[str, bytes]:
    """Caller holds the KB read lease; source metadata is not a semantic endorsement."""
    record = read_quality(kb_dir, artifact)
    base = artifact if artifact.is_dir() else artifact.parent
    name = artifact.name if artifact.is_dir() else artifact.stem
    paths = {kb_dir / relative for relative in files}
    paths.update(
        kb_dir / relative for relative in record.get("files", {}) if (kb_dir / relative).is_file()
    )
    contained_paths(kb_dir, list(paths))
    names = {
        path: name
        + "/"
        + (
            path.relative_to(base).as_posix()
            if path.is_relative_to(base)
            else "_support/" + path.relative_to(kb_dir).as_posix()
        )
        for path in paths
    }
    evidence_dir = "_evidence"
    while any(value.startswith(f"{name}/{evidence_dir}/") for value in names.values()):
        evidence_dir += "_"
    prefix = f"{name}/{evidence_dir}"
    references: dict[str, list[dict]] = {}
    for row in record.get("references", []):
        references.setdefault(row["target"], []).append(row)
    used = set()
    targets = {
        target: f"{prefix}/sources.html#evidence-{content_id(target)[:16]}" for target in references
    }
    for target, rows in references.items():
        if rows[0].get("asset"):
            targets[target] = f"{prefix}/images/{rows[0]['asset']}{Path(target).suffix}"
    payload = {}

    for path, filename in sorted(names.items()):

        def replace(value):
            parts = urlsplit(value)
            if parts.scheme or parts.netloc:
                return value
            local = (path.parent / unquote(parts.path)).resolve()
            portable = names.get(local)
            if local.is_relative_to(kb_dir / "wiki"):
                key = local.relative_to(kb_dir / "wiki").as_posix()
                key += "#" + unquote(parts.fragment) if parts.fragment else ""
                if key in targets:
                    used.add(key)
                    portable = targets[key]
            if portable is None:
                return value
            destination = posixpath.relpath(portable.split("#", 1)[0], posixpath.dirname(filename))
            fragment = portable.split("#", 1)[1] if "#" in portable else parts.fragment
            return quote(destination, safe="/.-") + ("#" + fragment if fragment else "")

        if path.suffix.lower() in {".md", ".markdown", ".html", ".htm"}:
            text = path.read_text(encoding="utf-8")
            text = (
                rewrite_html(text, replace)
                if path.suffix.lower() in {".html", ".htm"}
                else markdown_document(
                    text,
                    lambda value: markdown_links(value, replace),
                    lambda value: rewrite_html(value, replace),
                )
            )
            payload[filename] = text.encode()
        else:
            payload[filename] = path.read_bytes()

    sections = []
    exported = []
    for target in sorted(used):
        blocks = []
        for row in references[target]:
            reference = Evidence(**row["reference"])
            view = ParseStore(kb_dir).read(reference, max_chars=16000)
            source = SourceStore(kb_dir).version(reference.version_id)
            if row.get("asset"):
                if row["asset"] not in view.assets:
                    raise ValueError("Source image does not belong to the cited block")
                payload[targets[target]] = SourceStore(kb_dir).asset(row["asset"]).read_bytes()
            blocks.append(
                "<h3>"
                + html.escape(source.name)
                + "</h3><p>"
                + html.escape(json.dumps(view.location, ensure_ascii=False))
                + "</p><pre>"
                + html.escape(view.text)
                + "</pre><details><summary>Reader context</summary><pre>"
                + html.escape(view.context)
                + "</pre></details>"
            )
            exported.append(row)
        sections.append(
            f'<section id="evidence-{content_id(target)[:16]}">' + "".join(blocks) + "</section>"
        )
    page = (
        '<!doctype html><html><head><meta charset="utf-8"><title>Source evidence</title>'
        "<style>body{max-width:70em;margin:2em auto;font:16px sans-serif}"
        "pre{white-space:pre-wrap}section{margin:2em 0}</style></head><body>"
        "<h1>Original source excerpts</h1><p>These are retained parsed source ranges. "
        "Citation checks do not establish semantic support for every artifact claim. "
        "Reader context may contain parser annotations, not author statements.</p>"
        + "".join(sections)
        + "</body></html>"
    )
    payload[f"{prefix}/sources.html"] = page.encode()
    payload[f"{prefix}/quality.json"] = json.dumps(
        {"schema_version": 1, "quality": record, "exported_references": exported},
        indent=2,
        ensure_ascii=False,
    ).encode()
    notice = (
        "This export includes only explicitly bound source ranges referenced by these files.\n"
        "Format/citation checks do not establish complete semantic support or fact coverage.\n"
    )
    if record["status"] in {"unknown", "stale"} or record["checks"]["citations"] != "passed":
        notice += "Quality is unknown, stale or has issues; some references may be unavailable.\n"
    payload[f"{prefix}/README.txt"] = notice.encode()
    return payload
