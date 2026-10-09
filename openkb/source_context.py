"""Source associations and content nature, independent of generated claims."""

from __future__ import annotations

from typing import TYPE_CHECKING

from openkb import frontmatter

if TYPE_CHECKING:
    from openkb.application.query_views import QueryView

COMPILATION_FACT_RULES = """
Source fidelity applies to summaries, concepts, entities, updates and final rewrites.
Preserve the source's subject, units, conditions, strength and applicable scope.
A recommendation is not a requirement; one configuration is not every configuration.
Navigation titles, node summaries and existing generated pages help locate originals;
they do not establish facts. Source links identify associations, not factual verification.
Do not add guarantees, defaults, prohibitions, negative claims or operational rules
that the originals do not support. Do not fill omissions from general knowledge.
Silence about a conditional effect does not establish that other properties are
unchanged. Do not infer "only this changes", unaffected properties, or invariants
from independently listed facts. Retain the stated condition without widening it.
Ordinary arithmetic and unit conversions are allowed: show the derivation and any
necessary assumptions. Preserve conflicting source statements separately.
Distinguish source records from synthesis or interpretation where interpretation is
useful. External advice or background must be separately labelled, and is normally
omitted from document summaries. Do not add a list of irrelevant unknowns.
When evidence is limited, narrow the page to supported content or a navigation brief.
Existing generated assertions must be reconsidered against originals; do not inherit
an unsupported inference or promote it into a source fact during an update. Reassessment
is scoped to the originals actually read: missing windows do not refute old facts.
Preserve unrelated supported content, source conditions, supported calculations and
labelled synthesis; changed sources retain their old basis and pending refresh status.
"""

ANSWER_SOURCE_RULES = (
    """
Original sources establish document facts. Summaries, concepts, entities and navigation
are generated aids, even when they have valid source links. Before concluding specific
values, specifications, API parameters, commands, prices or operating conditions,
read the associated original locations. Read relevant images when extracted text is
insufficient. OCR is a transcription; generated visual descriptions and interpretations
are separate model output, not image text or source guarantees.
Use source-supported arithmetic and unit conversions, and provide fillable values with
their units when requested. Preserve conflicting conditions and identify the sources.
Search misses and partial reading do not prove absence; distinguish no matches from
an explicit source statement. State evidence limits only where the question needs them.
Answer directly and sufficiently without adding unsupported guarantees or defaults.
Keep independent source statements and their conditions separate. A condition that
mentions one change does not establish that other properties stay unchanged. Do not
add "still", "unaffected" or invariant claims unless the source supports that relation.
Historical user context explains intent, preferences and corrections; it does not prove
knowledge-base facts. Historical assistant text is not evidence.
If a reference such as "the second method" cannot be resolved from real user statements,
ask the user to identify it. Do not reconstruct omitted assistant answers or guess.
"""
    + """
Keep fields and conditions within their original heading, object, operation and source revision.
Unconfirmed section ancestry requires adjacent reading before combining conditions. Answer directly
when evidence is sufficient. Limit negative claims to actual coverage: distinguish not searched,
not read, a specific entry not explaining a fact, and an explicit statement of non-support.
An unread or failed image remains unknown independently of other images on the same page.
"""
)


def original_paths(view: QueryView, path: str, data: bytes | None = None) -> tuple[str, ...]:
    """Follow recorded summary/source associations using the bound reading snapshot."""
    from openkb.application.query_views import read_selected_file

    pending = [(path, data)]
    seen: set[str] = set()
    originals = []
    while pending:
        name, content = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        if name not in view.files:
            continue
        if name.startswith("sources/") and name.endswith((".md", ".json")):
            originals.append(name)
            continue
        if not name.endswith(".md"):
            continue
        metadata = frontmatter.parse((content or read_selected_file(view, name)).decode("utf-8"))
        links = metadata.get("sources", [])
        if not isinstance(links, list) or any(not isinstance(link, str) for link in links):
            raise ValueError("Invalid generated-page source associations")
        if pointer := metadata.get("full_text"):
            if not isinstance(pointer, str):
                raise ValueError("Invalid summary original-source pointer")
            links = [pointer, *links]
        pending.extend((link, None) for link in links)
    return tuple(sorted(set(originals)))


def source_reference(view: QueryView, path: str) -> dict:
    from openkb.source_reading import native_source

    source = native_source(view, path)
    return {
        **source,
        "reader": source["reading_action"]["tool"],
        "source_revision_id": view.source_revisions_by_path.get(path),
        "knowledge_revision_id": view.knowledge_revision_id,
        "content_digest": view.files[path],
    }


def reading_metadata(view: QueryView, path: str, data: bytes | None = None) -> dict:
    directory = path.partition("/")[0]
    kind = {
        "sources": "original_source",
        "summaries": "derived_summary",
        "concepts": "derived_concept",
        "entities": "derived_entity",
    }.get(directory, "navigation" if path == "index.md" else "generated_content")
    return {
        "content_kind": kind,
        "original_sources": [
            source_reference(view, name) for name in original_paths(view, path, data)
        ],
        "interpretation": (
            "Source associations are not factual verification; read originals for document facts."
            if kind != "original_source"
            else "Original text and native locators; OCR and generated image explanations keep "
            "their recorded nature. Reading a subset does not establish absence elsewhere."
        ),
    }
