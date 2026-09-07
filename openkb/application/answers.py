"""Save completed answers using the existing unique-name policy."""

from __future__ import annotations

from pathlib import Path

from openkb.locks import atomic_write_text, kb_ingest_lock


def save_exploration(kb_dir: Path, question: str, answer: str) -> Path | None:
    """Save a query answer to ``wiki/explorations/`` as a markdown page.

    Shared by the CLI ``query --save`` path and the REST ``/query?save`` path
    so both behave identically. Strips ghost wikilinks, generates a unique
    slug (with a CJK-safe fallback), and escapes the question for YAML
    frontmatter.
    """
    import hashlib
    import re

    from openkb.lint import list_existing_wiki_targets, strip_ghost_wikilinks

    if not answer:
        return None
    # Path allocation and the write share the KB mutation lock: otherwise two
    # concurrent REST saves can both select the same unused suffix and one
    # answer silently overwrites the other.
    with kb_ingest_lock(kb_dir / ".openkb"):
        explore_dir = kb_dir / "wiki" / "explorations"
        explore_dir.mkdir(parents=True, exist_ok=True)

        # Strip ghost wikilinks the agent may have emitted to non-existent
        # concept/summary pages -- the schema_md in the agent's instructions
        # encourages [[wikilinks]] but the agent's view of "which pages
        # exist" can drift from disk reality.
        known = list_existing_wiki_targets(kb_dir / "wiki")
        cleaned_answer, _ = strip_ghost_wikilinks(answer, known)

        slug = re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")[:60]
        if not slug:
            # CJK / punctuation-only questions collapse to an empty slug.
            # Fall back to a short hash so each question gets its own file.
            slug = hashlib.sha256(question.encode("utf-8")).hexdigest()[:12]
        explore_path = explore_dir / f"{slug}.md"
        # Uniquify to avoid clobbering an existing exploration with a colliding slug.
        counter = 1
        while explore_path.exists():
            explore_path = explore_dir / f"{slug}-{counter}.md"
            counter += 1

        # Escape the question for YAML frontmatter: wrap in double quotes and
        # escape backslashes and double quotes so questions containing `"` don't
        # produce invalid YAML.
        escaped = question.replace("\\", "\\\\").replace('"', '\\"')
        atomic_write_text(
            explore_path,
            f'---\nquery: "{escaped}"\n---\n\n{cleaned_answer}\n',
        )
    return explore_path
