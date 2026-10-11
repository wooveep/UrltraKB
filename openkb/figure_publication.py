"""Keep published figure identities before mutable normalization aliases are restored."""

import json
import re
from pathlib import Path

from openkb import frontmatter
from openkb.agent.page_figures import START, render_gallery, split_gallery
from openkb.agent.page_response import PageResponseError
from openkb.figure_assets import freeze_figures, relative_figure, source_identity
from openkb.locks import atomic_write_text
from openkb.markdown_images import apply_image_edits, image_references


def figure_independent_body(content: str) -> str:
    """Changing a figure URL/gallery does not establish a newer factual source."""
    body, _ = split_gallery(content)
    return apply_image_edits(
        body, [(r.start, r.end, r.relocated("original-figure")) for r in image_references(body)]
    ).strip()


def referenced_figure_inputs(wiki: Path, pages) -> set[str]:
    """Keep originals for historical pictures without treating them as current facts."""
    from openkb.figure_assets import FigureOccurrence

    inputs = set()
    for page in pages:
        if not page.endswith(".md"):
            continue
        _, entries = split_gallery((wiki / page).read_text())
        for entry in entries:
            figure = FigureOccurrence.model_validate(entry["figure"])
            if figure.processing_revision_id:
                inputs.add(figure.processing_revision_id)
    return inputs


def mentioned_figures(content, figures):
    """Recognize retained filenames in prose, never interpret fenced code as figures."""
    from openkb.content_blocks import markdown_layout

    tokens, _ = markdown_layout(content)
    fragments = [
        child.content
        for token in tokens
        for child in token.children or []
        if child.type in {"text", "code_inline"}
    ]
    aliases = {}
    for figure in figures:
        for alias in (figure.original_path, Path(figure.original_path).name):
            aliases.setdefault(alias, {})[figure.occurrence_id] = figure
    result = {}
    for alias, candidates in aliases.items():
        pattern = re.compile(r"(?<![\w./])" + re.escape(alias) + r"(?![\w./])")
        if not any(pattern.search(fragment) for fragment in fragments):
            continue
        if len(candidates) != 1:
            raise PageResponseError("figure_ambiguous_reference", f"Ambiguous figure: {alias}")
        result.update(candidates)
    return result


def preserve_page_figures(kb_dir: Path, wiki: Path, inputs: dict, *, source_wiki=None, pages=None):
    """Only uniquely bound retained references are repaired, in the candidate workspace."""
    source_wiki = source_wiki or wiki
    paths = (
        pages
        if pages is not None
        else [
            p.relative_to(wiki).as_posix()
            for section in ("summaries", "concepts", "entities")
            for p in (wiki / section).rglob("*.md")
        ]
    )
    catalogs = {}

    def associated(path, seen=None):
        seen = set() if seen is None else seen
        if path in seen:
            return []
        seen.add(path)
        target = source_wiki / path
        if not target.resolve().is_relative_to(source_wiki.resolve()):
            raise PageResponseError("figure_invalid_source", "Source association escapes Wiki")
        if not target.is_file():
            return []
        if path.startswith("sources/"):
            name = (
                Path(path)
                .name.removesuffix(".content.json")
                .removesuffix(".json")
                .removesuffix(".md")
            )
            if name not in catalogs:
                native = next(
                    (
                        f"sources/{name}{suffix}"
                        for suffix in (".content.json", ".json", ".md")
                        if (source_wiki / f"sources/{name}{suffix}").is_file()
                    ),
                    path,
                )
                text = (source_wiki / native).read_text("utf-8")
                raw = json.loads(text) if native.endswith(".json") else text
                catalogs[name] = freeze_figures(
                    source_wiki,
                    native,
                    raw,
                    source_identity(kb_dir, name, inputs=inputs),
                    destination=wiki,
                )
            return catalogs[name]
        meta = frontmatter.parse(target.read_text("utf-8"))
        links = meta.get("sources", [])
        if not isinstance(links, list) or any(not isinstance(link, str) for link in links):
            raise PageResponseError("figure_invalid_source", "Invalid page source list")
        if meta.get("full_text"):
            if not isinstance(meta["full_text"], str):
                raise PageResponseError("figure_invalid_source", "Invalid full_text pointer")
            links = [*links, meta["full_text"]]
        return [figure for link in links for figure in associated(link, seen)]

    for path in paths:
        text = (wiki / path).read_text("utf-8")
        if START in text:
            # Already content-addressed; its exact occurrences survive future restores.
            _, saved_entries = split_gallery(text)
            from openkb.figure_assets import FigureOccurrence, verify_figure

            for entry in saved_entries:
                verify_figure(wiki, FigureOccurrence.model_validate(entry["figure"]))
            continue
        references = image_references(text)
        figures = associated(path)
        edits, entries = [], {}
        for figure in mentioned_figures(text, figures).values():
            entries[figure.occurrence_id] = {
                "figure": figure.model_dump(mode="json"),
                "historical": False,
            }
        for reference in references:
            if reference.local_path is None:
                continue
            target = (wiki / path).parent / reference.local_path
            candidates = {
                f.occurrence_id: f
                for f in figures
                if (
                    target.resolve() == (wiki / f.original_path).resolve()
                    or reference.local_path == f.original_path
                    or reference.local_path == f.original_path.removeprefix("sources/")
                )
            }
            if len(candidates) != 1:
                raise PageResponseError(
                    "figure_ambiguous_reference", f"Cannot uniquely bind {path}: {reference.source}"
                )
            figure = next(iter(candidates.values()))
            edits.append(
                (
                    reference.start,
                    reference.end,
                    reference.relocated(relative_figure(wiki, path, figure.asset_path)),
                )
            )
            entries[figure.occurrence_id] = {
                "figure": figure.model_dump(mode="json"),
                "historical": False,
            }
        if entries:
            body = apply_image_edits(text, edits)
            atomic_write_text(wiki / path, render_gallery(wiki, path, body, list(entries.values())))
