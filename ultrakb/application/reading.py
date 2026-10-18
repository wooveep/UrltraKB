"""A committed page and its navigable source/link context under one read lease."""

from dataclasses import dataclass
from pathlib import Path

from ultrakb import frontmatter
from ultrakb.application.knowledge import KnowledgeEntry, list_knowledge
from ultrakb.application.pages import Page, read_page
from ultrakb.knowledge_scope import (
    KnowledgeScope,
    read_wiki_bytes,
    reading_scope,
    resolve_scope,
    wiki_page_paths,
)
from ultrakb.lint import _extract_wikilinks, _normalize_target


@dataclass(frozen=True)
class Reference:
    label: str
    path: str | None
    anchor: str = ""


@dataclass(frozen=True)
class PageContext:
    page: Page
    sources: tuple[Reference, ...]
    outlinks: tuple[Reference, ...]
    backlinks: tuple[Reference, ...]
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class KnowledgeContext:
    scope: KnowledgeScope
    entries: tuple[KnowledgeEntry, ...]
    context: PageContext | None


def read_knowledge_context(
    kb_dir: Path,
    path: str | None = None,
    *,
    view_id: str = "legacy",
    publication_id: str | None = None,
    scope: KnowledgeScope | None = None,
) -> KnowledgeContext:
    """Prepare a whole refresh before replacing any part of the displayed knowledge."""
    from ultrakb.application.views import published_scope

    if scope is None:
        scope = published_scope(kb_dir, view_id, publication_id=publication_id)
    else:
        scope = resolve_scope(kb_dir, scope)
        if scope.view_id != view_id or (
            publication_id is not None and publication_id != scope.publication_id
        ):
            raise ValueError("Knowledge refresh requires one explicitly selected publication")
    with reading_scope(scope):
        entries = list_knowledge(kb_dir, scope=scope)
        if path is None:
            path = entries[0].path if entries else None
        try:
            context = read_page_context(kb_dir, path, scope=scope) if path else None
        except FileNotFoundError:
            context = read_page_context(kb_dir, entries[0].path, scope=scope) if entries else None
        return KnowledgeContext(scope, entries, context)


def read_knowledge_image(kb_dir: Path, path: str, *, scope: KnowledgeScope) -> tuple[bytes, str]:
    """Read a raster image from the same fixed wiki as the displayed body."""
    selected = resolve_scope(kb_dir, scope)
    media_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }
    suffix = Path(path).suffix.lower()
    if suffix not in media_types:
        raise ValueError("Unsupported knowledge image format")
    with reading_scope(selected):
        return read_wiki_bytes(selected, path), media_types[suffix]


def read_page_context(
    kb_dir: Path, path: str, *, scope: KnowledgeScope | None = None
) -> PageContext:
    scope = resolve_scope(kb_dir, scope)
    with reading_scope(scope):
        page = read_page(kb_dir, path, scope=scope)
        known = {
            Path(path).with_suffix("").as_posix(): path
            for path in wiki_page_paths(scope)
            if path.endswith(".md")
        }
        index: dict[str, set[str]] = {}
        for target in known:
            for key in (target, Path(target).name):
                index.setdefault(_normalize_target(key), set()).add(target)

        def reference(raw: str, *, source=False) -> Reference:
            target, _, anchor = raw.partition("#")
            target = target.removesuffix(".md")
            candidates = (
                [f"sources/{target}", f"summaries/{target}", target] if source else [target]
            )
            for candidate in candidates:
                if candidate in known:
                    return Reference(raw, candidate, anchor)
                matches = index.get(_normalize_target(candidate), set())
                if len(matches) == 1:
                    return Reference(raw, next(iter(matches)), anchor)
            return Reference(raw, None, anchor)

        source_values = frontmatter.parse(page.content).get("sources", [])
        if isinstance(source_values, str):
            source_values = [source_values]
        source_refs = []
        for raw in source_values if isinstance(source_values, list) else ():
            if not isinstance(raw, str):
                continue
            resolved = ()
            if scope.knowledge is not None:
                from .storage_selection import page_source_paths

                resolved = page_source_paths(
                    scope.reading_view, page.path + ".md", raw, native=False
                )
            if resolved:
                source_refs.extend(
                    Reference(raw, Path(path).with_suffix("").as_posix(), raw.partition("#")[2])
                    for path in resolved
                )
            else:
                fallback = reference(raw, source=True)
                if (
                    scope.knowledge is not None
                    and fallback.path is not None
                    and fallback.path + ".md" in scope.reading_view.native_inputs
                ):
                    fallback = Reference(raw, None, fallback.anchor)
                source_refs.append(fallback)
        sources = tuple(source_refs)
        outlinks = tuple(reference(raw) for raw in sorted(set(_extract_wikilinks(page.body))))
        backlinks = []
        problems = []
        for target, file in sorted(known.items()):
            if target == page.path or target.split("/")[0] == "sources":
                continue
            if Path(file).name in {"AGENTS.md", "SCHEMA.md", "log.md"}:
                continue
            try:
                content = read_wiki_bytes(scope, file).decode("utf-8")
            except (OSError, UnicodeError) as exc:
                problems.append(f"无法读取 {target}（{type(exc).__name__}）")
                continue
            if any(reference(raw).path == page.path for raw in _extract_wikilinks(content)):
                backlinks.append(Reference(target, target))
        return PageContext(page, sources, outlinks, tuple(backlinks), tuple(problems))
