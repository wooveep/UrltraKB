"""Single-task instructions for navigation overview and explicit page selection."""

OVERVIEW_OUTPUT_RULES = """Return only the Markdown overview in the requested language,
without frontmatter, code fences or a page plan."""

OVERVIEW_RULES = (
    """You are compiling this document into a wiki knowledge base.
Use the supplied whole-document PageIndex structure and node summaries.

Write a concise overview that captures the document's key themes and findings.
This overview will be used to select concept and entity pages.

"""
    + OVERVIEW_OUTPUT_RULES
)

TOPIC_OVERVIEW_RULES = (
    """You are compiling this document into a wiki knowledge base.
Summarize the selected topic group using its supplied PageIndex node summaries.
Use the coarse whole-document directory to understand this group's place in the document.
Capture the group's key themes and findings for later concept and entity selection.

"""
    + OVERVIEW_OUTPUT_RULES
)

OVERVIEW_MERGE_RULES = (
    """Reorganize and compress the supplied partial summaries into
a concise overview of the document's key themes and findings for concept and entity
selection. Make it strictly shorter than the combined summaries; do not append them
or fill in chapters not supplied in those summaries.

"""
    + OVERVIEW_OUTPUT_RULES
)

PAGE_SELECTION_RULES = """Based on the overview above, decide how to update the wiki's CONCEPT pages
and ENTITY pages. Use the supplied PageIndex and existing-page catalogue.

A CONCEPT is an abstract, recurring idea, pattern or mechanism.
An ENTITY is a specific named thing: a person, organization, place, product,
named work or event. Each name goes in exactly one group. A subject may have
both a concept and a related entity; they cross-link rather than merge.

Rules:
- Use the supplied KB stage and concept_selection_guidance; do not infer KB stage
  from a projected or empty displayed catalogue.
- Create an entity page only when the entity is central to this document
  or likely to recur across sources. Do not page proper nouns mentioned only
  in passing. Roughly 5–15 entities per document is typical; fewer for sparse
  documents.
- Mention in author credits, publication metadata or acknowledgments alone does not
  justify a page; a person or work substantively studied in the body may qualify.
- A product type does not automatically fit each command, feature or mechanism.
- Prefer update over create for an existing concept or entity.
- Do not create a concept or entity that overlaps an existing page.
- Do not create concepts that are just the document topic itself.
- Related means lightweight cross-linking only, without a content rewrite."""

PAGE_OUTPUT_RULES = """Return a small Markdown page list, grouped into Concepts and Entities.
Within each group, distinguish Create, Update and Related. Combined headings such as
Create concepts and Update entities are also welcome. Omit unused groups or mark them
empty. Related contains existing pages to cross-link only; it does not create or rewrite
a page. Put deferred or non-selected objects in Notes.

For each Create/Update entry give a readable Title or Name. The group supplies its
concept/entity classification; no repeated Kind field is needed. For an entity, give
Type from the supplied configuration. A short labelled Purpose is optional.
Use a supplied catalogue title or target for updates. File paths are assigned by the
application. Exact source locations are not required for page selection; select
worthwhile pages even when precise locations are not yet known.

When the supplied PageIndex already shows relevant chapters, carry their existing
titles or section keys into a short Subject field under the selected page. This is
a retrieval hint, not a separate analysis task: reuse known navigation labels;
leave it out when uncertain. Such hints and external-reference notes are optional.
Do not produce an exact source-range or prerequisite-dependency plan in this task;
original retrieval is a later step. Unlinked external references are allowed, and their
unread contents must not be expanded. Do not write page bodies, another overview or JSON.
An empty selection is valid."""

PAGES_RULES = PAGE_SELECTION_RULES + "\n\n" + PAGE_OUTPUT_RULES
# Compatibility exports: source format no longer selects a different page-selection goal.
PDF_SELECTION_RULES = PAGE_SELECTION_RULES
PDF_OUTPUT_RULES = PAGE_OUTPUT_RULES
PDF_PAGES_RULES = PAGES_RULES

TOPIC_PAGES_RULES = (
    "Select within the target topic group in the global context, "
    "preserving earlier accepted suggestions in the carry."
)


def planning_rules(
    subtask: str, target_kind: str = "", navigation_style: str = "", runtime=None
) -> str:
    """Select only the current task; runtime data stays in the structured payload.

    These templates reference the existing inputs rather than interpolate them.
    Source/model text, including braces or placeholder-like tokens, is never rendered.
    """
    if subtask == "overview":
        return {
            "topic_summary": TOPIC_OVERVIEW_RULES,
            "overview_merge": OVERVIEW_MERGE_RULES,
        }.get(target_kind, OVERVIEW_RULES)
    if subtask == "page_sources":
        from openkb.agent.document_page_sources import RULES

        return RULES
    if subtask != "pages":
        raise ValueError("Invalid Markdown planning subtask")
    from openkb.agent.document_planning_runtime import selection_guidance

    rules = PAGES_RULES + ("\n\n" + TOPIC_PAGES_RULES if target_kind == "topic_group" else "")
    return rules + selection_guidance(runtime) if runtime else rules
