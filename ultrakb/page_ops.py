"""Wiki-page mutations for the Workbench: delete a concept/entity page.

Reuses the compile/lint machinery so a deletion degrades cleanly rather than
leaving dangling references:

- inbound ``[[wikilinks]]`` in other pages are demoted to plain text
  (``lint.fix_broken_links``), not left pointing at a gone page;
- the page's ``index.md`` entry (itself a ``[[wikilink]]`` line) is removed
  outright (``compiler.remove_doc_from_index``).

All writes run under the KB ingest lock (crash-safe serial mutation, matching
``ultrakb remove``); the underlying rewrites use ``atomic_write_text``.
"""

from __future__ import annotations

from pathlib import Path

from ultrakb import frontmatter
from ultrakb.knowledge_scope import KnowledgeScope, resolve_scope
from ultrakb.lint import (
    _EXCLUDED_FILES,
    _extract_wikilinks,
    _normalize_target,
    _read_md,
    fix_broken_links,
    list_existing_wiki_targets,
    strip_ghost_wikilinks,
)
from ultrakb.locks import durable_unlink

# Compiled page types the user may EDIT (a recompile can still regenerate them):
# concept/entity synthesis PLUS per-document summaries. index/log/reports are
# generated artifacts and stay read-only.
EDITABLE_SECTIONS = ("concepts", "entities", "summaries")
# DELETABLE is narrower: a summary is bound to its source document, so it is
# removed by deleting the DOCUMENT (/api/v1/remove), never on its own — deleting
# it alone would leave a doc with no summary.
DELETABLE_SECTIONS = ("concepts", "entities")


def validate_page_ref(
    path: str, *, allowed: tuple[str, ...] = EDITABLE_SECTIONS
) -> tuple[str, str]:
    """Split a ``'<section>/<name>'`` page ref into ``(section, stem)``.

    ``allowed`` is the permitted-section allowlist — defaults to the editable
    set; delete passes the narrower :data:`DELETABLE_SECTIONS`. Guards against
    path traversal: ``section`` must be in ``allowed`` and ``stem`` a single safe
    filename segment (no separators, ``..``, or leading dot). Raises ValueError.
    """
    parts = path.strip().strip("/").split("/")
    if len(parts) != 2:
        raise ValueError("page ref must be '<section>/<name>' (e.g. concepts/attention)")
    section, stem = parts
    if section not in allowed:
        raise ValueError(f"section must be one of {allowed}, got {section!r}")
    if not stem or stem in (".", "..") or stem.startswith(".") or "\\" in stem:
        raise ValueError(f"invalid page name: {stem!r}")
    return section, stem


def pages_linking_to(wiki: Path, target_norm: str, *, exclude: Path) -> list[str]:
    """Content pages whose ``[[wikilinks]]`` resolve to ``target_norm`` (backlinks).

    Excludes ``exclude`` (the target page itself), ``index.md`` (handled by the
    index-entry removal, not link demotion), ``reports/`` + ``sources/``, and
    :data:`lint._EXCLUDED_FILES` (AGENTS.md/SCHEMA.md/log.md) — the generated
    docs lint never rewrites; scanning them would list the shipped schema doc as
    a backlink and then let ``fix_broken_links`` corrupt it.
    Returns relative ``'section/stem'`` refs, sorted, for a stable impact preview.
    """
    hits: set[str] = set()
    exclude_resolved = exclude.resolve()
    for md in wiki.rglob("*.md"):
        rel = md.relative_to(wiki)
        if rel.parts[:1] in (("reports",), ("sources",)):
            continue
        if md.name == "index.md" or md.name in _EXCLUDED_FILES or md.resolve() == exclude_resolved:
            continue
        for raw in _extract_wikilinks(_read_md(md)):
            if _normalize_target(raw) == target_norm:
                hits.add(str(rel.with_suffix("")).replace("\\", "/"))
                break
    return sorted(hits)


def _on_disk_stem(section_dir: Path, stem: str) -> str:
    """The actual on-disk stem for ``stem``.

    A case-insensitive filesystem (macOS/Windows) may store the real filename
    with different case than the ref the caller resolved (e.g. opened via a
    ``[[concepts/foo]]`` wikilink while the file is ``Foo.md``). index.md entries
    use the on-disk name, so the exact-match index cleanup needs the real stem.
    """
    if not section_dir.is_dir():
        return stem
    ci_match = stem
    for p in section_dir.glob("*.md"):
        if p.stem == stem:  # exact — prefer it (case-sensitive FS, both variants)
            return stem
        if p.stem.lower() == stem.lower():
            ci_match = p.stem
    return ci_match


def page_link_context(kb_dir: Path, path: str, *, scope: KnowledgeScope | None = None) -> dict:
    """Outbound + inbound links for a page — the edit-impact panel.

    ``outlinks`` are the distinct pages this page links to (resolvable
    ``[[wikilinks]]``); ``backlinks`` are the pages that link to it. Editing the
    BODY does not break either (links are path-based), so this is context, not a
    blocker. Returns ``status`` ``not_found`` when the page is absent.
    """
    scope = resolve_scope(kb_dir, scope)
    section, stem = validate_page_ref(path)
    wiki = scope.wiki_dir
    page = wiki / section / f"{stem}.md"
    target = f"{section}/{stem}"
    if not page.is_file():
        return {"status": "not_found", "target": target, "outlinks": [], "backlinks": []}

    target_norm = _normalize_target(target)
    norm_known = {_normalize_target(t): t for t in list_existing_wiki_targets(wiki)}
    out: set[str] = set()
    for raw in _extract_wikilinks(_read_md(page)):
        canon = norm_known.get(_normalize_target(raw))
        if canon and _normalize_target(canon) != target_norm:
            out.add(canon)
    backlinks = pages_linking_to(wiki, target_norm, exclude=page)
    return {"status": "ok", "target": target, "outlinks": sorted(out), "backlinks": backlinks}


def delete_page_files(wiki: Path, section: str, stem: str, backlinks: list[str]) -> tuple[int, int]:
    """Apply the existing deletion rewrites to a protected or disposable Wiki."""
    from ultrakb.agent.compiler import remove_doc_from_index

    real_stem = _on_disk_stem(wiki / section, stem)
    durable_unlink(wiki / section / f"{stem}.md", missing_ok=True)
    remove_doc_from_index(
        wiki,
        "",
        concept_slugs_deleted=[real_stem] if section == "concepts" else [],
        entity_slugs_deleted=[real_stem] if section == "entities" else [],
    )
    restrict = [wiki / f"{ref}.md" for ref in backlinks] + [wiki / "index.md"]
    return fix_broken_links(wiki, restrict_to=restrict)


def edited_page_content(existing: str, body: str, known_targets: set[str]) -> tuple[str, list[str]]:
    """Keep code-managed metadata and accept the entire supplied body verbatim."""
    parts = frontmatter.split(existing)
    metadata = parts[0] if parts else ""
    cleaned, ghosts = strip_ghost_wikilinks(body, known_targets)
    return metadata + cleaned, sorted(set(ghosts))
