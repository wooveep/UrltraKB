"""Reconcile only pending source contributions, reusing completed document products."""

import json

from ultrakb import frontmatter
from ultrakb.locks import atomic_write_text
from ultrakb.refresh_publication import (
    RefreshedPage,
)
from ultrakb.state import HashRegistry


def _full_windows(reader):
    """Every yielded range must match the frozen original, including its unread tail."""
    selected, _ = reader._document_selection(reader.original_path)
    text = selected["content"]
    result, cursor = [], 0
    for window in reader.iter_document_windows(fits=lambda payload: True):
        start = window["selection_offset"]
        end = start + len(window["content"])
        if start < cursor or text[cursor:start].strip() or text[start:end] != window["content"]:
            raise ValueError("Selected source evidence is not contiguous")
        cursor = end
        result.append(window)
    if text[cursor:].strip() or not result:
        raise ValueError("Selected source evidence is unread")
    return result


async def _reconcile_page(
    kb_dir, view, page, identities, retained, config, bundle, readers, *, link_registry=None
):
    from ultrakb.selected_refresh_evidence import retained_reader
    from ultrakb.selected_refresh_manual import generated_refresh_baseline

    wiki = view.scope.wiki_dir
    evidence = []
    for identity in identities:
        if identity not in readers:
            reader = retained_reader(
                wiki, retained[identity], kb_dir=kb_dir, view_id=view.scope.view_id
            )
            readers[identity] = reader, _full_windows(reader)
        evidence.extend(readers[identity][1])
    reader = readers[identities[0]][0]
    await reconcile_page(
        wiki,
        page,
        reader,
        evidence,
        config,
        bundle,
        link_registry=link_registry,
        generated_baseline=generated_refresh_baseline(kb_dir, view, page),
    )
    return RefreshedPage(
        input_revisions=identities,
        base_digest=view.base_pages[page],
        candidate_digest=HashRegistry.hash_file(wiki / page),
        original_files={
            p: HashRegistry.hash_file(wiki / p)
            for identity in identities
            for p in retained[identity].paths
        },
    )


async def reconcile_page(
    wiki, page, reader, evidence, config, bundle, *, link_registry=None, generated_baseline=None
):
    """Coordinate a frozen page against complete originals under either persistence binding."""
    from ultrakb.agent.compiler import _SYSTEM_TEMPLATE, _format_known_targets
    from ultrakb.agent.page_figures import START
    from ultrakb.agent.page_originals import PageOriginalContext
    from ultrakb.agent.page_updates import PageUpdate, UnchangedPage
    from ultrakb.agent.update_generation import generate_page_update
    from ultrakb.config import DEFAULT_CONFIG, resolve_entity_types
    from ultrakb.lint import list_existing_wiki_targets
    from ultrakb.schema import get_agents_md

    context = PageOriginalContext(
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "Complete frozen originals for this page's current contributors:\n"
                    + json.dumps(evidence, ensure_ascii=False)
                    + "\nReconcile the old draft with these originals. Preserve every supported "
                    "condition, procedure and qualification. Source documents remain distinct; "
                    "one version's rule does not establish another version's rule. Generated "
                    "summaries and the old draft are not evidence. Return block-edit JSON.",
                }
            ],
        },
        True,
        True,
        tuple(evidence),
    )
    path = wiki / page
    original = path.read_bytes().decode("utf-8")
    parts = frontmatter.split(original)
    if parts is None:
        raise ValueError("Shared page has no valid frontmatter")
    targets = set(link_registry.paths) if link_registry else list_existing_wiki_targets(wiki)
    figures = reader.page_figures(evidence, long=True)
    patch = PageUpdate(
        original,
        known_targets=targets,
        allowed_types=frozenset(resolve_entity_types(config))
        if page.startswith("entities/")
        else None,
        source_ranges=figures.ids if figures.figures or START in original else None,
    )
    from ultrakb.selected_refresh_manual import (
        editable_refresh_blocks,
        manual_refresh_fragments,
    )

    if generated_baseline is not None:
        patch.allowed_blocks = editable_refresh_blocks(patch, generated_baseline)
        patch.preserved_fragments = manual_refresh_fragments(patch, generated_baseline)
    result = await generate_page_update(
        patch=patch,
        originals=context,
        windows=reader,
        page_path=path,
        model=config.get("model", DEFAULT_CONFIG["model"]),
        bundle=bundle,
        system_message={
            "role": "system",
            "content": _SYSTEM_TEMPLATE.format(
                schema_md=get_agents_md(wiki), language=config.get("language", "en")
            ),
        },
        current_original=None,
        link_registry=link_registry,
        known_targets_message={
            "role": "user",
            "content": "Existing wiki targets:\n" + _format_known_targets(targets),
        },
        task_message={
            "role": "user",
            "content": f"Refresh the existing page {page} against its complete source evidence. "
            "Correct unsupported or superseded statements and incorporate necessary "
            "current contributions. Retain other supported content and attribution.",
        },
    )
    if not isinstance(result, UnchangedPage):
        metadata = parts[0]
        for key in ("description", "type"):
            if key in result:
                metadata = frontmatter.set_line(metadata, key, result[key])
        atomic_write_text(path, metadata + result["content"])


def _refresh_index(wiki):
    """The index is a deterministic catalog; no document or model is rerun."""
    from ultrakb.agent.compiler import _update_index
    from ultrakb.schema import INDEX_SEED

    old = (wiki / "index.md").read_text("utf-8") if (wiki / "index.md").exists() else ""
    # Retain the existing exploration section, whose content is not regenerated here.
    tail = old[old.index("## Explorations") :] if "## Explorations" in old else "## Explorations\n"
    atomic_write_text(wiki / "index.md", INDEX_SEED.split("## Explorations")[0] + tail)
    concepts = {
        p.stem: frontmatter.parse(p.read_text("utf-8")).get("description", "")
        for p in sorted((wiki / "concepts").glob("*.md"))
    }
    entities = {
        p.stem: (
            frontmatter.parse(p.read_text("utf-8")).get("type", "other"),
            frontmatter.parse(p.read_text("utf-8")).get("description", ""),
        )
        for p in sorted((wiki / "entities").glob("*.md"))
    }
    for path in sorted((wiki / "summaries").glob("*.md")):
        metadata = frontmatter.parse(path.read_text("utf-8"))
        _update_index(
            wiki,
            path.stem,
            list(concepts),
            doc_brief=metadata.get("description", ""),
            concept_briefs=concepts,
            doc_type=metadata.get("doc_type", "short"),
            entity_names=list(entities),
            entity_meta=entities,
        )
