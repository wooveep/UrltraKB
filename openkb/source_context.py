"""Source associations and content nature, independent of generated claims."""

from __future__ import annotations

from typing import TYPE_CHECKING

from openkb import frontmatter

if TYPE_CHECKING:
    from openkb.application.query_views import QueryView

COMPILATION_FACT_RULES = """
Compose summaries, concepts, entities and updates as source-grounded references.
Organize description and body by named subject, applicable conditions and recorded
facts or procedures. Let the evidence determine the page's breadth. Preserve original
names and document titles unless sources supply aliases; translations may accompany
them. Describe relationships only as specifically as the sources establish them.
Navigation and generated pages locate originals; links do not verify their claims.
Match the source's units, revision, scope and logical strength: requirements,
recommendations, permissions and explicit exclusions are different facts. Independent
facts do not establish causation, unchanged properties, guarantees or unstated defaults.
Ordinary arithmetic and unit conversions are allowed: show premises and derivation;
keep derived conclusions within the calculated bounds. Preserve conflicting source
statements separately. Distinguish useful synthesis from recorded facts. Include a
local evidence gap only when needed to interpret or use the page's actual subject.
External advice requires a separate label and is normally omitted from summaries.
Reassess existing generated assertions against the originals actually read; do not
inherit unsupported inferences. Missing windows do not refute old facts. Preserve
unrelated supported content, its conditions, calculations and labelled synthesis;
changed sources retain their old basis and pending refresh status.
"""

ANSWER_SOURCE_RULES = """
Original sources establish document facts. Summaries, concepts, entities and navigation
are generated aids, even when they have valid source links. Read the associated originals
for specific values, specifications, API parameters, commands, prices and conditions.
Claims about what a document includes or omits require the same original support.
Identify the necessary parts of the user's question and answer each supported part.
Lead with the requested result and the evidence needed to use it. Let the question
determine the answer's length and structure; finish when its necessary parts are covered.
Build the answer around those requested facts: bind each fact to its object, operation,
condition and source location before explaining it. For document questions, each
paragraph should supply a requested fact, a derivation with its premises, or a local
evidence limit. Broader practical advice belongs only in answers that request it.
When a necessary part lacks evidence, read its particular condition, section or other
source within the shared budget. Follow continuation actions for unread ranges; a title
match or complete returned range does not establish semantic coverage of the question.
Continue until the necessary parts are supported or a specific local gap remains.
Explain an evidence limit when it affects a requested conclusion, a necessary condition
or the user's ability to use the answer. Keep the limit local and retain the supported
details, explanations and conclusions in the rest of the answer.
Explain reasons, steps, comparisons, calculations or examples when the question needs
them. Preserve useful synthesis and conditional advice, distinguishing document facts
from analysis or reliable background where the distinction matters. Respect a request
to use documents only. Give source-supported arithmetic with inputs, units and derivation.
For a recommendation derived from a calculation, compare the user's requirement against
the calculated bounds, retaining the same assumptions and units. Explain a recommendation
using the attributes actually established in the sources. Label necessary assumptions and
hypothetical examples as such, rather than adding them to the source's meaning.
Provide fillable values when asked.
Keep facts and conditions within their original heading, object, operation and revision;
read adjacent context when section ancestry is uncertain. Preserve conflicting statements
and identify their sources. Match the source's strength: a recommendation remains a
recommendation, and a condition describing one change supports only the stated relation.
Use explicit source support for guarantees, defaults, unchanged properties and negative
rules. Report a search miss or reading limit as an observation about that search or range.
For absence, uniqueness or completeness conclusions, use explicit source support at that
scope or a complete, verifiable enumeration of the relevant bounded domain. A file catalog
enumerates files; it does not certify everything in their contents. Apply a source's
negative statements to their stated object, conditions and revision.
Original figures are retained for the reader; this text model does not interpret pixels.
When text cannot establish a figure-only relationship, keep that local gap explicit.
A retained historical figure is not evidence for the current page body. OCR is a transcription;
generated visual interpretations are separate model output. An unread or failed image
remains unknown independently of other images on the same page.
Historical user context explains intent, preferences and corrections; it does not prove
knowledge-base facts. Historical assistant text is not evidence.
If a reference such as "the second method" cannot be resolved from real user statements,
ask the user to identify it. Do not reconstruct omitted assistant answers or guess.
"""


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
