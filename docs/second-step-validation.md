# Second-step validation — issue #51

Validated on Linux with the repository's existing pinned environment, against
starting commit `43ec74db052a7daa510ce2586a49211fd15094ab`.

## Saved-output handoff

Tests register real immutable sources, run native parsing and PageIndex saving,
then consume the saved originals through `indexed_reader`, `source_units` and
`read_evidence_group`. The original input can be removed after saving; recovery
and next-step reads do not reparse it.

Covered formats include Markdown ATX/Setext headings, lists, code, tables,
frontmatter and owned inline/reference images; quoted multiline CSV records;
HTML DOM positions and image-bearing headings/tables; XML namespaces,
attributes and mixed content; and native PDF positions/bookmarks. Existing
DOCX, PPTX and XLSX position tests remain part of the regression suite.

The repository README was also passed through an isolated, offline second-step
handoff: 125 saved blocks, 37 navigation nodes and 125 downstream source units
with 23,495 original characters. Navigation enhancement was disabled for that
sample. This proves saved-output consumption, not model structure quality.

Controlled HTTP model tests exercise sequential windows, empty continuation,
dense output above 2,048 tokens, explicitly small output budgets, truncation,
one local positioning attempt, repeated titles, shared-block hierarchy,
TOC/body boundaries, unresolved-entry persistence and recovery. They capture
actual transport messages to compare the fixed system and evidence prefix
between structure/summary and generation/verification tasks.

The 200,000-token assembly regression serializes and counts real evidence:
the first window contains between 190,000 and 200,000 tokens, the tail uses a
second window, and the persisted descriptors total under 4 KB. It does not
measure provider cache hit rate, production latency, peak RSS or semantic accuracy.
No authenticated provider experiment or paid cache warmup was performed.

## Validation results

- Ruff checks and formatting checks passed; mypy passed for 373 modules.
- Final staged patch applied to a clean checkout of the starting commit:
  **70 passed** in 73.88 seconds. This includes the new handoff regressions and
  every task-related failure found by the full run, while excluding unrelated
  dependency edits already present in the main workspace.
- One full-suite run: 2,539 passed, 51 failed, 2 skipped in 18 minutes.
  The run began before the final review corrections; targeted reruns below
  validate the subsequent fixes instead of repeating the entire suite.
- Of those 51 failures, 24 reproduce at the starting commit. Three further
  fingerprint assertions are caused by the pre-existing, unstaged
  `dependency_preflight` changes: they fail in the original workspace and pass
  when this patch alone is applied to the clean starting commit.
- The other 24 failures were addressed: updated navigation/configuration/wire
  expectations, restored Markdown progress, preserved the audited generation
  compatibility stamp, and supplied sufficient fixture budgets for the fixed
  full context contract. Multipart coverage, correction, citation, sharing,
  and complete-request capacity assertions remain in place.

The pre-existing failures are: eight embedded/nested OCR resume cases; six
local embedded-document failure cases; one embedded DOCX source-context case;
one attachment-progress case; one client-cleanup case; one quote/context-role
case; two body-lifecycle upgrade cases; one legacy-draft bridge case; and three
context-lifetime upgrade cases. They are outside this change and remain unfixed.

The fixed system increases request overhead. Explicit small context/output and
request-count limits continue to be enforced. The multipart fixtures now use
6,144 context tokens where 4,096 cannot hold the full contract, and the table
relationship fixture allows 40 requests for the additional bounded batches;
these are test settings, not changes to user budgets.

## Standards

No blocking documented-standard or correctness findings remain after review.
The reviewer noted possible Data Clumps in the navigation request arguments,
a non-blocking design judgement. A frontmatter string resembling a Markdown
image can still create an intake missing-image warning; this predates the change.

## Spec

The reviewer found no remaining concrete findings in the corrected paths.
Regression tests cover all reported ordering, TOC occurrence/boundary,
HTML image/container and shared-block hierarchy defects, including retained
unresolved TOC status after a successful empty body fallback.

Final review: Standards — no blocking findings, two non-blocking notes;
Spec — no remaining findings in reviewed corrections.
