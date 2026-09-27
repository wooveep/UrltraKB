"""Task-only instructions for tolerant cumulative planning."""

OVERVIEW_RULES = """Return an updated COMPLETE concise document overview in Markdown,
incorporating the current target into the supplied cumulative overview. Use the
PageIndex structure and summaries for navigation, and the supplied original text for
content, limits, conditions and reference requirements. Do not claim to have read
external or attached material. Preserve important prior themes and limits; the new text
replaces the old snapshot. Describe topic, scenarios, knowledge organization, prerequisites
and unread material. Prefer one main heading and a few thematic paragraphs, roughly
800–1800 Chinese characters or equivalent information in the requested language; this is
guidance, not a limit. Avoid reproducing revision tables, account values, long commands
or itemized model lists. Processed ranges record execution, not proven semantic coverage;
clipped or missing prior input remains a limitation. Only perform the overview task;
do not include page plans or bodies. Return prose, not JSON or reasoning."""

PAGES_RULES = """Select useful concept and entity pages for the whole source after
overview traversal. The frozen planning_context contains the available overview,
global PageIndex branches and existing catalogue, all derived guidance, not evidence.
Use the detailed navigation for the current global or topic-group target. Organize
coherent reusable knowledge across sections: integrate related prerequisites, main
procedure and recovery when they answer one purpose. Do not make a page for every
step, isolated parameter, example interface name or signature. Prefer a substantive
core product/entity and independently useful concepts; preserve distinct versions,
platforms and scenarios when independently useful. A runtime source_conditions
diagnostic belongs in Notes, not a knowledge page, unless the source itself teaches
that subject. State page-creation conditions explicitly; a conditional author index
suggestion is not an unconditional recommendation. Mark explicit extensions with
the existing title and added purpose/location clues. Do not automatically promote a
conditional suggestion merely by extending it. No fixed page count is required.
Return readable Markdown lists or tables
with names/titles, kinds and optional location clues. Headings and field labels are
flexible. A supplied section_key, full heading path, original heading or precise
keyword can help retrieval later. Locations are suggestions, not evidence receipts:
unknown locations may remain unresolved. Distinguish subject, prerequisite context,
and related clues. Suggestions may concern another section of this saved source;
do not claim to have read material outside the supplied original text.
Use supplied entity types when applicable. Include purpose or reference hints if
helpful, and preserve qualifications or uncertainty in notes. Each page should have an
independent reusable purpose. Prefer complete procedures with prerequisites over pages
for every step or table row. A person mentioned only in a signature or revision log,
or an isolated parameter, rarely warrants a page without substantive information.
Reuse confirmed existing suggestions and add location clues or purpose details to them;
an explicit title-based extension is sufficient. The cumulative catalogue may be compact
or incomplete: omitted does not mean nonexistent. Preserve meaningful version/platform
differences. If classification is uncertain, say so without guessing a concept category.
The separate overview task handles Summary/Overview; only suggest concept
or entity pages here. External document names without supplied bodies remain
reference hints. No source block must be routed to a page. Do not write page bodies,
internal paths, proofs, JSON or reasoning. If no new page is warranted, state that
explicitly. Never invent unread external or attachment details."""
