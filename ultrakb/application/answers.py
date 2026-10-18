"""Save completed answers using the existing unique-name policy."""

from __future__ import annotations

from pathlib import Path

from ultrakb.knowledge_scope import KnowledgeScope, resolve_scope
from ultrakb.locks import atomic_write_text, kb_ingest_lock
from ultrakb.mutation import mutation_scope


def exploration_slug(question: str, *, unique: bool) -> str:
    import hashlib
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")[:60]
    return (
        hashlib.sha256(question.encode("utf-8")).hexdigest()[:12] if not slug and unique else slug
    )


def exploration_markdown(question: str, answer: str, known: set[str]) -> str:
    import json

    from ultrakb.lint import strip_ghost_wikilinks

    cleaned_answer, _ = strip_ghost_wikilinks(answer, known)
    return f"---\nquery: {json.dumps(question, ensure_ascii=False)}\n---\n\n{cleaned_answer}\n"


def save_exploration(
    kb_dir: Path,
    question: str,
    answer: str,
    *,
    unique: bool = True,
    scope: KnowledgeScope | None = None,
) -> Path | None:
    """Save a query answer to ``wiki/explorations/`` as a markdown page.

    Shared by the CLI ``query --save`` path and the REST ``/query?save`` path
    so both behave identically. Strips ghost wikilinks, generates a unique
    slug (with a CJK-safe fallback), and escapes the question for YAML
    frontmatter. ``unique=False`` retains the CLI's original slug/overwrite
    policy, including its empty CJK slug.
    """
    scope = resolve_scope(kb_dir, scope, writable=True)
    from ultrakb.lint import list_existing_wiki_targets

    if not answer:
        return None
    # Path allocation and the write share the KB mutation lock: otherwise two
    # concurrent REST saves can both select the same unused suffix and one
    # answer silently overwrites the other.
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        explore_dir = scope.wiki_dir / "explorations"
        explore_dir.mkdir(parents=True, exist_ok=True)

        # Strip ghost wikilinks the agent may have emitted to non-existent
        # concept/summary pages -- the schema_md in the agent's instructions
        # encourages [[wikilinks]] but the agent's view of "which pages
        # exist" can drift from disk reality.
        known = list_existing_wiki_targets(scope.wiki_dir)
        slug = exploration_slug(question, unique=unique)
        explore_path = explore_dir / f"{slug}.md"
        # Uniquify to avoid clobbering an existing exploration with a colliding slug.
        counter = 1
        while unique and (explore_path.exists() or explore_path.is_symlink()):
            explore_path = explore_dir / f"{slug}-{counter}.md"
            counter += 1

        with mutation_scope(kb_dir, [explore_path], operation="save-answer"):
            atomic_write_text(
                explore_path,
                exploration_markdown(question, answer, known),
            )
    return explore_path
