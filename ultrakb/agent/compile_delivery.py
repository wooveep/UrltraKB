"""Publish validated compilation results and navigation without invoking a model."""

from collections.abc import Sequence
from pathlib import Path

from ultrakb import frontmatter
from ultrakb.agent.compile_plan import CompilePlan
from ultrakb.agent.page_updates import UnchangedPage
from ultrakb.locks import atomic_write_text


def publish_compile_results(
    wiki_dir: Path,
    doc_name: str,
    plan: CompilePlan,
    *,
    summary: str,
    concept_results: Sequence[tuple[str, str | UnchangedPage, bool, str] | BaseException] = (),
    entity_results: Sequence[tuple[str, str | UnchangedPage, str, str] | BaseException] = (),
    doc_brief: str = "",
    doc_type: str = "short",
    rewrite_summary: bool = False,
    original_path: str | None = None,
) -> None:
    """Commit generated pages or retain their prior versions after delivery failures."""
    import sys

    from ultrakb.agent.compiler import (
        _backlink_concepts,
        _backlink_entities,
        _backlink_summary,
        _backlink_summary_entities,
        _sanitize_concept_name,
        _update_index,
        _write_concept,
        _write_entity,
        _write_summary,
        check_model_stop,
        list_existing_wiki_targets,
        logger,
        raise_if_terminal,
        report_compile_issue,
        strip_ghost_wikilinks,
    )

    check_model_stop()
    # Validate every proposed alias before any page/summary write. Names are
    # metadata for these exact planned paths; aliases never create new paths.
    plan_aliases = {}
    for kind in ("concepts", "entities"):
        group = getattr(plan, kind)
        for item in [*group.create, *group.update]:
            if "aliases" not in item:
                continue
            aliases = frontmatter.alias_values(item["aliases"])
            name = _sanitize_concept_name(item["name"])
            path = wiki_dir / kind / f"{name}.md"
            if path.is_file():
                frontmatter.merge_aliases(path.read_bytes().decode("utf-8"), aliases)
            plan_aliases[(kind, name)] = aliases
    accepted_aliases: set[tuple[str, str]] = set()
    source_file = f"summaries/{doc_name}.md"
    related_items = plan.concepts.related
    entity_related = plan.entities.related
    planned_slugs = {
        _sanitize_concept_name(c["name"]) for c in plan.concepts.create + plan.concepts.update
    } | {_sanitize_concept_name(s) for s in related_items}
    entity_planned = {
        _sanitize_concept_name(e["name"]) for e in plan.entities.create + plan.entities.update
    } | {_sanitize_concept_name(s) for s in entity_related}
    known_targets = (
        list_existing_wiki_targets(wiki_dir)
        | {f"concepts/{s}" for s in planned_slugs}
        | {f"entities/{s}" for s in entity_planned}
        | {f"summaries/{doc_name}"}
    )
    results, entity_results = list(concept_results), list(entity_results)
    total, etotal = len(results), len(entity_results)
    concept_names: list[str] = []
    concept_briefs_map: dict[str, str] = {}
    pending_writes: list[tuple[str, str, bool, str]] = []
    entity_names: list[str] = []
    entity_meta: dict[str, tuple[str, str]] = {}
    entity_pending: list[tuple[str, str, str, str]] = []

    if results:
        failure_types: list[str] = []
        unchanged = 0
        for concept_result in results:
            if isinstance(concept_result, BaseException):
                raise_if_terminal(concept_result)
                if not isinstance(concept_result, Exception):
                    raise concept_result
            if isinstance(concept_result, Exception):
                logger.warning("Concept generation failed: %s", concept_result)
                failure_types.append(type(concept_result).__name__)
                continue
            name, page_content, is_update, brief = concept_result
            accepted_aliases.add(("concepts", _sanitize_concept_name(name)))
            if isinstance(page_content, UnchangedPage):
                unchanged += 1
                report_compile_issue("page_update_unchanged")
                logger.info("Concept %s unchanged: %s", name, page_content.reason)
                continue
            pending_writes.append((name, page_content, is_update, brief))
            safe_name = _sanitize_concept_name(name)
            concept_names.append(safe_name)
            if brief:
                concept_briefs_map[safe_name] = brief

        # Include exception type names inline so the stdout line is
        # self-contained — per-failure WARNINGs go to stderr.
        written = len(pending_writes)
        logger.info(
            "Concept results: %d written, %d unchanged, %d planned", written, unchanged, total
        )
        if written + unchanged < total:
            report_compile_issue("concept_generation_incomplete", "concepts")
            reason = ", ".join(sorted(set(failure_types))) if failure_types else "see log (stderr)"
            sys.stdout.write(
                f"    [WARN] {total} concept(s) planned but only {written} written "
                f"for {doc_name} ({reason}).\n"
            )
            sys.stdout.flush()

    if entity_results:
        entity_failure_types: list[str] = []
        unchanged = 0
        for entity_result in entity_results:
            if isinstance(entity_result, BaseException):
                raise_if_terminal(entity_result)
                if not isinstance(entity_result, Exception):
                    raise entity_result
            if isinstance(entity_result, Exception):
                logger.warning("Entity generation failed: %s", entity_result)
                entity_failure_types.append(type(entity_result).__name__)
                continue
            name, page_content, brief, etype = entity_result
            accepted_aliases.add(("entities", _sanitize_concept_name(name)))
            if isinstance(page_content, UnchangedPage):
                unchanged += 1
                report_compile_issue("page_update_unchanged")
                logger.info("Entity %s unchanged: %s", name, page_content.reason)
                continue
            entity_pending.append((name, page_content, brief, etype))

        ewritten = len(entity_pending)
        logger.info(
            "Entity results: %d written, %d unchanged, %d planned", ewritten, unchanged, etotal
        )
        if ewritten + unchanged < etotal:
            report_compile_issue("entity_generation_incomplete", "entities")
            reason = (
                ", ".join(sorted(set(entity_failure_types)))
                if entity_failure_types
                else "see log (stderr)"
            )
            sys.stdout.write(
                f"    [WARN] {etotal} entity(ies) planned but only {ewritten} written "
                f"for {doc_name} ({reason}).\n"
            )
            sys.stdout.flush()

    # Strip ghost wikilinks from entity bodies and write each page.
    for name, page_content, brief, etype in entity_pending:
        safe = _sanitize_concept_name(name)
        is_update = (wiki_dir / "entities" / f"{safe}.md").exists()
        cleaned, ghosts = (
            (page_content, []) if is_update else strip_ghost_wikilinks(page_content, known_targets)
        )
        if ghosts:
            logger.info(
                "stripped %d ghost wikilink(s) from entity %s: %s",
                len(ghosts),
                name,
                ghosts[:5],
            )
        _write_entity(
            wiki_dir,
            name,
            cleaned,
            source_file,
            is_update,
            brief=brief,
            type_=etype,
            preserve_body=is_update,
        )
        entity_names.append(safe)
        entity_meta[safe] = (etype, brief)

    # Strip unresolved wikilinks from concept bodies before writing. The
    # whitelist includes existing files + this round's planned slugs +
    # the summary for this document.
    for i, (name, page_content, is_update, brief) in enumerate(pending_writes):
        cleaned, ghosts = (
            (page_content, []) if is_update else strip_ghost_wikilinks(page_content, known_targets)
        )
        if ghosts:
            logger.info(
                "stripped %d ghost wikilink(s) from concept %s: %s",
                len(ghosts),
                name,
                ghosts[:5],
            )
        pending_writes[i] = (name, cleaned, is_update, brief)

    # Navigation is assembled below; keep the first summary's statements intact.
    if rewrite_summary:
        final_summary, _ = strip_ghost_wikilinks(summary, known_targets)
        _write_summary(
            wiki_dir,
            doc_name,
            final_summary,
            doc_type=doc_type,
            description=doc_brief,
            full_text=original_path if doc_type != "short" else None,
        )

    # --- Write concept pages to disk ---
    for name, page_content, is_update, brief in pending_writes:
        _write_concept(
            wiki_dir,
            name,
            page_content,
            source_file,
            is_update,
            brief=brief,
            preserve_body=is_update,
        )

    # A model may correctly leave the body unchanged while the accepted global
    # identity pass learns another name. Persist that name without adding a new
    # source contribution or rewriting the page body.
    for kind, name in sorted(accepted_aliases):
        aliases = plan_aliases.get((kind, name), [])
        if not aliases:
            continue
        check_model_stop()
        path = wiki_dir / kind / f"{name}.md"
        text = path.read_bytes().decode("utf-8")
        updated = frontmatter.merge_aliases(text, aliases)
        if updated != text:
            atomic_write_text(path, updated)

    # --- Step 3b: Process related items (code only, no LLM) ---
    sanitized_related = [_sanitize_concept_name(s) for s in related_items]

    # --- Step 3c: Backlink — summary ↔ concepts (code only) ---
    all_concept_slugs = concept_names + sanitized_related
    if all_concept_slugs:
        _backlink_summary(wiki_dir, doc_name, all_concept_slugs)
        _backlink_concepts(wiki_dir, doc_name, concept_names)

    # --- Step 3d: Process entity related items + backlinks (code only) ---
    # Pure navigation belongs to the new summary; it contributes no facts to old pages.
    entity_related_slugs = [
        slug
        for slug in (_sanitize_concept_name(s) for s in entity_related)
        if (wiki_dir / "entities" / f"{slug}.md").is_file()
    ]

    entity_backlink_slugs = entity_names + entity_related_slugs
    if entity_backlink_slugs:
        _backlink_summary_entities(wiki_dir, doc_name, entity_backlink_slugs)
        _backlink_entities(wiki_dir, doc_name, entity_names)

    # --- Step 4: Update index (code only) ---
    _update_index(
        wiki_dir,
        doc_name,
        concept_names,
        doc_brief=doc_brief,
        concept_briefs=concept_briefs_map,
        doc_type=doc_type,
        entity_names=entity_names,
        entity_meta=entity_meta,
    )
