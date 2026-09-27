"""Single-task instructions for navigation overview and explicit page selection."""

OVERVIEW_RULES = """Using the supplied whole-document PageIndex structure and node
summaries, write a concise Markdown overview: the document's subject, main knowledge
areas and their relationships, useful scenarios, prerequisites, limitations and
external reference requirements. Prefer global organization over section-by-section
retelling. Do not enumerate long commands, every interface or operational parameter.
Node summaries and partial_summaries are derived navigation, not original evidence.
Only supplied original excerpts are direct evidence. Preserve source uncertainty and
do not expand unread attachments or external material. The application reports input
omissions, clipping and execution progress; do not speculate about those in the text.
For a topic_summary, summarize only the selected disjoint branch with the coarse
whole-document directory as context. For overview_merge, synthesize and compress the
provided partial summaries into a strictly shorter overview; do not append them.
Prefer one main heading and a few thematic paragraphs, roughly 800–1800 Chinese
characters or equivalent information in the requested language; this is guidance,
not an acceptance gate. Only complete the overview task. Return readable Markdown,
without page plans, JSON, page bodies or reasoning."""

PAGES_RULES = """Use the overview, whole-document PageIndex and existing knowledge
catalogue to choose worthwhile pages to create or update. These are derived guidance,
not proof that the originals were read. For a topic-group target, consider its detail
in the same global context and preserve earlier accepted suggestions in the carry.
Concept pages answer an independent question, explain a mechanism or cover a complete
task. Entity pages describe central or reusable objects. A chapter, signature, default
parameter or example name does not by itself warrant a page. No fixed page count or
one-page-per-section allocation is required. Integrate prerequisites, procedures and
exceptions serving one purpose across sections; preserve useful platform/version
boundaries. Prefer confirmed existing pages; use a recognizable catalogue target for
updates and explain uncertainty instead of guessing a target to overwrite.

Use explicit Markdown regions: Create pages / 创建页面, Update pages / 更新页面,
and Notes / 说明. Put only pages you have decided to create or update in those sets.
A create candidate needs a Title/名称 and Kind/类型 (concept or a supplied entity type).
Purpose/用途, Selection/主体章节, Necessary context/必要上下文 and External references/
外部参考 are optional: provide them when supported, never invent values to fill columns.
Tables and lists are both welcome. Put non-created, deferred, conditional and background
objects in Notes. Operational prerequisites within a chosen page remain that page's
notes; they do not defer creation of the page. The receiver follows the explicit sets;
it does not infer creation decisions from arbitrary negations or conditions in notes.

Location clues may be a supplied stable section key, full title/path, unique heading
number or valid same-branch heading-number range. They guide later original retrieval;
do not claim unprovided originals have been read. Distinguish subject, necessary
context and optional related clues. Preserve free notes and external requirements;
unlinked external references are allowed and their unread contents must not be expanded.
Use a confirmed existing suggestion title for an explicit extension, preserving its
identity. Do not merge merely similar subjects or mistake omitted catalogue entries
for nonexistent pages. Only select pages; do not write bodies, another overview, JSON,
internal generated paths, reasoning or a per-block coverage ledger. An empty explicit
create/update set is a valid result."""
