"""A committed page and its navigable source/link context under one read lease."""

from dataclasses import dataclass
from pathlib import Path

from openkb import frontmatter
from openkb.application.pages import Page, read_page
from openkb.lint import _extract_wikilinks, _normalize_target
from openkb.locks import kb_read_lock


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


def read_page_context(kb_dir: Path, path: str) -> PageContext:
    with kb_read_lock(kb_dir / ".openkb"):
        page = read_page(kb_dir, path)
        wiki = (kb_dir / "wiki").resolve()
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
