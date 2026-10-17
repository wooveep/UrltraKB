"""A committed page and its navigable source/link context under one read lease."""

from dataclasses import dataclass
from pathlib import Path

from ultrakb import frontmatter
from ultrakb.application.knowledge import KnowledgeEntry, list_knowledge
from ultrakb.application.pages import Page, read_page
from ultrakb.knowledge_scope import KnowledgeScope, reading_scope, resolve_scope
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
) -> KnowledgeContext:
    """Prepare a whole refresh before replacing any part of the displayed knowledge."""
    from ultrakb.application.views import published_scope

    scope = published_scope(kb_dir, view_id, publication_id=publication_id)
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
    target = (selected.wiki_dir / path).resolve()
    if Path(path).is_absolute() or not target.is_relative_to(selected.wiki_dir):
        raise ValueError("Invalid image path")
    if target.suffix.lower() not in media_types:
        raise ValueError("Unsupported knowledge image format")
    with reading_scope(selected):
        return target.read_bytes(), media_types[target.suffix.lower()]


def read_page_context(
    kb_dir: Path, path: str, *, scope: KnowledgeScope | None = None
) -> PageContext:
    scope = resolve_scope(kb_dir, scope)
    with reading_scope(scope):
        page = read_page(kb_dir, path, scope=scope)
        wiki = (scope.wiki_dir).resolve()
        known = {
            p.relative_to(wiki).with_suffix("").as_posix(): p
            for p in wiki.rglob("*.md")
            if p.is_file() and p.resolve().is_relative_to(wiki)
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
        sources = (
            tuple(reference(raw, source=True) for raw in source_values if isinstance(raw, str))
            if isinstance(source_values, list)
            else ()
        )
        outlinks = tuple(reference(raw) for raw in sorted(set(_extract_wikilinks(page.body))))
        backlinks = []
        problems = []
        for target, file in sorted(known.items()):
            if target == page.path or target.split("/")[0] == "sources":
                continue
            if file.name in {"AGENTS.md", "SCHEMA.md", "log.md"}:
                continue
            try:
                content = file.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                problems.append(f"无法读取 {target}（{type(exc).__name__}）")
                continue
            if any(reference(raw).path == page.path for raw in _extract_wikilinks(content)):
                backlinks.append(Reference(target, target))
        return PageContext(page, sources, outlinks, tuple(backlinks), tuple(problems))
