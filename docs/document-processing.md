# Document execution

Imports use one isolated task runtime in the CLI, REST API and desktop. A task
captures settings before document processing begins and keeps that configuration
for the rest of its batch. Updating settings applies to a new task.

## Results and stopping

Each document reports `source_intake`, `knowledge_compilation`, `stage`, `reason`,
`quality`, `unfinished`, `resources`, `warnings`, `omissions`, `coverage` and `usage`. A completed or
previously completed identical document is successful. Exhausted local content
attempts can exclude that content and publish verified siblings; invalid model
output never becomes published knowledge. Global execution budgets still stop the run. An ordinary failed
or unfinished document allows the batch to continue; stopping ends the current
item and remaining items. Already committed documents remain committed.

`openkb add` exits with 0 for a successful batch, 1 for failed or unfinished work,
and 130 after a user interrupt. Parameter errors retain Click's validation
behavior. Ctrl+C requests stopping and waits for worker exit and recovery.

The REST `POST /api/v1/add` accepts an optional 32-character lowercase hexadecimal
`task_id` form field. Generate it before submitting if querying before the result
is needed. The ID is bound to the knowledge base and uploaded names and bytes:
repeating the same submission observes that task, and conflicting input returns
409. JSON results and SSE `result` events use the same result structure. The first
SSE `start` event includes the ID; `progress` events precede `result` and `done`.

The API uses one process per task-history directory; a second API worker fails
at startup instead of accepting tasks it cannot coordinate.

`GET /api/v1/tasks/{task_id}` reads persisted execution status.
`POST /api/v1/tasks/{task_id}/stop` requests stopping. Observe until the task is
terminal and `processes_reaped` is true; `stop_confirmed` is only true for a
confirmed stopped task. Ending a connection ends observation and leaves the task
running within its budget. Uploaded input remains owned until execution ends.
An interrupted process with an unconfirmed result is reported explicitly and is
never automatically replayed from its summary. A confirmed business receipt
survives failed auxiliary teardown; cleanup warnings are separate from knowledge
completion.

## Accepted publication policy — 2026-09-13

> 导入文档，异常的情况，可以丢弃，知识可以缺失，任务不能随意中止与判断失败。

The subsequent accepted requirement also keeps ordinary content exhaustion on the
normal import completion path: retain a registered, editable source and omission
notice even when no knowledge can be generated. Users may fill gaps afterward.
The implementation now uses the same publication transaction even when no facts,
plan or verified topic survives. The source summary states zero generated knowledge,
keeps omissions visible, and remains editable. See the
[repair and small-file validation plan](import-repair-plan.md) for implementation
order, exact acceptance cases and issue closure conditions. New validation uses
small fixed files; full large-document reruns are not a prerequisite for this work.

Skipping means excluding unreliable content or candidate knowledge from this import,
while retaining originals, historical evidence and other sources' contributions.
Bounded recovery continues independent work. No usable candidate still completes
source registration with zero knowledge and partial coverage. Cancellation, hard
budgets and integrity failures still stop execution. Unknown remote OCR jobs are
never blindly resubmitted; their pending transcription does not block publication.

`coverage` reports the immutable original block ranges and assets, independently of
publication: `unknown` for legacy records, `pending` before publication, and `partial`
or `complete` after publication. A saved image, available OCR transcription and image
understanding have separate states. Pending necessary image understanding remains a
gap. Ranges cannot be removed or shortened to claim completeness. Published source
tools report the published parse's coverage, even if a newer parse is selected.
Desktop, CLI and Continue retain the distinction between committed knowledge and
remaining original content.

The user explicitly approved normal import and publication despite ordinary
errors and missing content. This revises the publication requirements in
[document ingestion specification #13](https://github.com/wooveep/UrltraKB/issues/13)
and [OCR specification #22](https://github.com/wooveep/UrltraKB/issues/22).

After the original is retained, ordinary conversion, parsing or OCR failures and
local omissions must not block publication of usable, source-verifiable content.
This includes missing body sections or appendices, tables, images, related assets,
notes and embedded documents in DOCX, PDF and other supported formats. Users do
not need to repair or individually confirm these ordinary problems before the
available knowledge can be published.

Successful publication may retain `warnings` and `needs_review` diagnostics.
`knowledge_compilation: completed` means the available content was compiled and
committed; it does not claim that the entire original was parsed. Task results
and the published summary identify known omissions. Missing material must not be
invented or treated as proof that a fact does not exist. Review and reprocessing
remain available afterward, with original bytes and evidence identities retained.

The selected knowledge changes still commit through one managed transaction.
Unverifiable source identity, service/account failures, exhausted execution budgets,
unaccepted new manual overwrites, user stopping and failed transaction recovery
retain their existing outcomes. A byte-identical regenerated contribution is a no-op:
Continue preserves manual edits without demanding acceptance of an unchanged candidate. Unsupported generated claims are excluded,
never published. This is an accepted specification revision; it does not
claim that every format and error path has already passed implementation checks.


### Conservative compilation omissions — accepted revision

The latest acceptance rule is “可以少，不能错误”: uncertain content may be
excluded instead of requiring document-specific debugging. Existing bounded
validation retries, splitting, one correction and configured adjudication remain
in place. A valid rejection is retained; repeated import must not reroll it into
acceptance.

- Fact extraction excludes the entire original block if an isolated unit still
  fails, including otherwise successful split siblings from that block.
- Planning excludes only exhausted isolated input members. It never repairs a
  plan by inventing membership or silently accepting missing members.
- Generation excludes the entire failed topic, so partially verified steps do
  not appear as a complete task. Retained content still requires original
  evidence, full coverage of its selected facts and semantic verification.
- If no usable facts, plan or verified topic remains, source registration and the
  editable zero-knowledge summary still complete. Confirmed temporary service
  failures use bounded backoff, then isolate affected content. Unknown model execution,
  account failures, cancellation, identity corruption and global budgets retain their
  execution limits; pending OCR content is independently recorded as an omission.

`omissions` contains stage, fixed reason and excluded identities. The original
source/version/parse identifies the evidence; rejected text is not copied into
the summary. The summary explains the coverage limit, and the publication
transaction stores the same omissions with its document registration. Navigation
links to excluded or withdrawn pages become plain display text; code examples
remain unchanged. Older contributions from this source are withdrawn when no
longer verified for the new version; other sources and manual-change protection
remain effective.

An ordinary identical import returns the prior publication and omissions without
new model calls. Explicit **Continue** retries excluded work, reusing valid
extraction and whole-topic receipts when their evidence, grouping, other-source
content and verification contract are unchanged. A new parse, model or contract
may invalidate reuse. Desktop stages show exclusions while publication remains
completed, and Continue remains available. Success describes the available
verified content, not full original-document coverage.

## General import optimization contract

Production import decisions must follow document structure, source identity,
available evidence and explicit runtime budgets. Validation filenames, source
hashes, paragraph numbers, vendor names and expected page counts must not select
special processing paths. Format adapters may handle structural differences;
semantic examples and regression fixtures do not become document-specific rules.

Publication and source-link audits do not establish complete semantic recall or
perfect accuracy. Performance comparisons must also report source-grounded
coverage and known omissions. Excluding a prerequisite must not make a retained
procedure or conclusion misleading. Before partial publication, a bounded dependency
review checks candidates against the complete available original and recorded parsing
omissions. Dependent or unresolved candidates are also excluded, including transitive
dependencies. Capacity or protocol failures split the candidates while preserving
the complete ordered original and omissions in each request. Minimum candidates
that cannot be reviewed remain pending; with no usable candidate, source registration
and the zero-knowledge summary still complete. Split recovery and valid refusals
survive Continue.

Measure cold import, identical reimport and interrupted continuation separately.
Compare the same enabled features and quality scope, including all failed,
retried, corrected and verified requests in actual token usage. Keep production
defaults separate from validation overrides. A successful large-document test
does not establish general throughput; independent documents and fixed semantic
anchors are required to evaluate general improvements.

## Source indexing and original citations

Markdown, text, PDF, DOCX, XLSX and PPTX use the same import and Continue operations.
Each parsed version gets a complete ordered source index before facts are extracted.
Native Markdown/DOCX headings, PDF physical pages, spreadsheet sheets/cells and slide
objects keep their own position types. Spreadsheet values retain formula text, merge
relationships and declared table headers; formulas are not executed. Undeclared column
roles remain uncertain. Unsupported slide objects and notes remain explicit omissions
or original attachments; successful parsing does not prove semantic completeness.

Source indexing is enabled by default. Every new index uses the original local
PageIndex collection and SQLite storage at `.openkb/pageindex.db`. The SDK stores
its document tree and original block text there; source/version/parse bindings
and reusable index lookups live in the same database. Facts, generation, verification,
question and chat read indexed originals through PageIndex's collection API, in
batches of at most 1,000 native ranges. Native range numbers are mapped back to the
actual paragraph, cell, slide or physical page before citations are returned.

`navigation.enabled: false` disables paid enhancement while still creating the
database index with complete basic ranges. A missing or damaged published database
index requires rebuilding; it does not silently switch to raw parsing or a JSON tree.
The release does not read, migrate or fall back to
`.openkb/source-store/navigation/*.json`. The source store still retains immutable
originals, parsing, assets and usage receipts. Index storage and retrieval use the
database regardless of document length or the former PDF threshold.

Native structure is preferred. Missing structure and long range summaries may use
bounded model requests: at most 32 requests, 262144 tokens and 120 seconds for optional
index work, further bounded by its configured allowance and the remaining import
budget. Import reserves at least four requests for necessary compilation and limits
indexing to at most 10% of remaining finite tokens. Small ranges retain an original-text
preview. Exhausted optional allowance leaves a basic or degraded index; account,
identity, cancellation and global budget failures still end the operation.

Question and chat tools capture the published source/version/parse/index together.
Provider length stops are retained even when the model SDK would otherwise discard
them. The application may request one concise replacement using the evidence already
read, with no tools and at most one model turn. The repair instruction is not saved
in conversation history. A second truncated response cannot be saved as a completed
answer. Legacy query and TTY chat also reject truncated completion; prior completed
turns and all request charges remain intact.

Source links in final answers must be observed in tool evidence or derived from a
complete binding with its matching source page or snapshot anchor in the same tool
result. Abbreviated
paths and invented anchors use their own bounded citation replacement allowance,
separate from recovery after length stops. Markdown code examples are not treated
as citations. This target check does
not establish that a cited source supports every factual claim; semantic review
remains separate. Terminal answers use the final response, excluding intermediate
tool narration.

Use the returned original-range citations for details, prerequisites and exceptions;
titles and summaries are navigation hints. Long original and Markdown reads paginate
explicitly. A source-index rebuild reads saved parsing, changes only its retrieval
index, and does not rewrite compiled knowledge or existing citations. Unpublished or
mismatched parsing cannot silently replace published evidence.
Navigation degradation describes the index hints, separately from parsing quality.
Original-range tools provide exact image links tied to each block's validated published
assets. Missing, damaged or symbolically linked images remain unavailable while text can
still be read; image destinations must not be constructed from document names.

## Reuse, continuation and measured cost

Fact extraction, range structure, summaries, topic planning, generation and verification
keep distinct reusable results. Sharing requires equal complete inputs, including
ordered batch context, subjects, versions, headings, neighboring evidence, table roles,
candidate metadata, model options and validation rules. Request-local short identities
and repeated context references reduce transport size while original text and every
occurrence retain their evidence identity. Each adopting source receives its own binding.

Topic-planning batches read one frozen existing-knowledge catalogue. A bounded second
pass coordinates ambiguous candidate identities; uncertain groups retain separate,
stable names and complete membership. Planning runs concurrently within the existing
document budget. Page writes stay serialized. Generation and verification reuse are
independent: a verification change can recheck a valid draft without regenerating it.
Current evidence, coverage, structure and publication checks still apply to every hit.

The successful request's actual output cap is recorded separately from the configured
adaptive policy. A concurrent expansion cannot relabel another response. One executor
owns an equivalent analysis throughout its adaptive retries; waiters can stop without
cancelling that executor. Unknown remote work after process loss is not claimed to have
executed exactly once. Current-source completion receipts can resume under their
unchanged policy; new shared requests must match actual effective request parameters.

New checkpoints store complete contracts; local stage indexes can be rebuilt from
immutable records. Bounded historical adapters may recover drafts, but an old draft
cannot grant publication permission. Corrupt shared records become local misses and
are retained when repaired. History cleanup accounts for source bindings, current
publications, pending work and historical chat citations before removing unused data.
Source updates retract withdrawn contributions while retaining contributions belonging
to other sources and protecting manual edits.

Usage includes failed requests, retries, indexing, corrections and verification.
Source history adds standalone index rebuilds once; indexing already included in an
import is not added twice. Question/chat usage is reported separately. Measurements
include stage spans, preparation/queue/request time, request IDs, concurrency and
shared-analysis hits/bindings. Overlapping spans are not summed as elapsed time.
Provider cache tokens are separate from local reuse and do not imply free requests.
Absent provider details remain unknown; SDK-normalized zero cache counts are also
unknown when the original field cannot be distinguished from absence.

Default correction remains bounded to one repair plus configured review. Valid
rejections persist, title-only repair preserves body text, and uncertain local
dependencies require full affected-candidate review. Additional concurrency within a
single topic remains disabled pending repeatable evidence of a useful tail-latency gain.

## Default execution profile

New and existing knowledge bases automatically inherit a processing profile;
no manual setup or configuration migration is required. The desktop's
**Settings → Processing budgets** shows the effective values. A complete
`processing` mapping in global configuration overrides the built-in profile,
and a knowledge-base mapping overrides the global profile. Clearing an override
restores inheritance. Reading settings does not write defaults into a library.

| Field | Default | Meaning |
| --- | --- | --- |
| `context_tokens` | 262144 (256K) | Initial total context cap for one complete request |
| `output_tokens` | 131072 (128K) | Initial output reserve within that context cap |
| `max_context_tokens` | 1048576 (1M) | Model context ceiling for adaptive retries |
| `max_output_tokens` | 393216 (384K) | Model output ceiling for adaptive retries |
| `request_timeout` | 180 | Seconds per request without an explicit timeout |
| `stage_timeout` | `null` | Optional total seconds for one processing stage; no default cap |
| `document_timeout` | `null` | Optional total seconds for the whole document; no default cap |
| `cleanup_timeout` | 10 | Seconds for auxiliary cleanup or shutdown grace |
| `max_attempts` | 2 | Maximum transport attempts at each request size |
| `max_requests` | `null` | Optional cumulative model-attempt limit; no default cap |
| `max_tokens` | null | No cumulative document token ceiling; a positive override includes outstanding reservations |
| `concurrency` | 8 | Maximum concurrent model calls within one document |

These are starting allowances and explicit ceilings, not measured model capacities or a
guarantee that every document will finish in one run. Models with smaller context
or output capacities need lower request caps. Explicit malformed or incomplete
overrides still produce configuration errors before a model request; they are
never silently replaced by defaults. Use the same profile when comparing runs.

A valid explicit per-request timeout retains existing configuration precedence;
remaining stage and document time always bound it. The complete request includes
system instructions, document content, prior knowledge, tools, schema and the
output reserve. Requests reserve their input estimate and maximum output before
sending. Available usage settles that reservation; missing usage remains charged
at the reserved amount and is labeled unknown. Cached-token details do not imply
free usage. No prompt is silently truncated and no alternate model is selected.
SDK retry settings are disabled at the request boundary. The execution controller
retries transient transport/service errors within the configured limits.
Observable attempts and unknown internal transport attempts are distinct fields.

A confirmed `finish_reason: length` discards that response and increases request
allowances from 256K/128K to 512K/256K, then 1M/384K (K = 1024 tokens). The increased
allowances remain in effect for the rest of that document operation; the next
document starts at the initial values. At the ceiling, fact extraction, topic
planning, generation and verification retry smaller batches. A single long
source span can be split at exact character positions while retaining heading,
neighbor and asset associations. An indivisible unit that still truncates remains
unfinished. Partial JSON and unverified knowledge are never saved as completed
checkpoints, and retries do not advance business progress. Every attempted request
still records usage, including truncated responses and unknown reservations.
Cancellation, elapsed time and request-count limits still apply across retries.
If a generated candidate makes the next verification or correction request too
large, the context allowance also grows before falling back to batch splitting.
Explicit per-operation output caps remain binding. Older complete profiles that
omit the two new ceiling fields retain their original request caps; clear their
override to inherit this profile, or set explicit ceilings for that model.

Within one document, fact extraction runs up to `concurrency` batches at a
time (default 8). Independent knowledge pages use at most four workers, bounded
by that same setting and the number of pages. Each page's generation, correction
and verification stay ordered. All workers share model allowances, cancellation
and elapsed-time controls. The lease owner captures validated original evidence
before parallel generation; worker reads use detached text and metadata, avoiding
cross-thread KB lock waits. Wiki writes and final publication remain serialized.

A persistent local response defect records pending work while other independent
units or pages finish and save checkpoints. Cancellation, transport uncertainty,
budget exhaustion and storage/integrity errors still stop admission and cancel
siblings. Facts are restored to source order before planning. Checkpoint files and
their catalogue are serialized so concurrent completion cannot lose entries.
The setting limits simultaneous requests; it is not an RPM quota and does not
multiply context caps or the document-wide request allowance. Explicit global or
knowledge-base concurrency overrides remain in effect until cleared.

Topic planning uses at most 128 candidate topics per batch, with additional input
and output capacity checks. Completed batches advance a topic counter; retries do
not inflate it. Planning remains ordered because later batches reuse earlier page
identities. This reduces large response bursts but does not guarantee a provider
will respond within a particular time.

Semantic review responses are recorded separately from accepted page checkpoints.
The record binds the exact candidate, evidence, prompt, model options, endpoint
identity and attempt. On resume, the current validator rechecks the recorded
response; a valid rejection cannot become approval merely because the same draft
was sent to the model again. Changed content or evidence receives a new review.
Still-invalid and uncertain responses retain bounded retries. A raw response
record is never sufficient for publication: coverage, located feedback and the
supported publication receipt must still validate.

Planning groups parameters, prerequisites, steps and exceptions into stable
functional or deployment-task pages while retaining independent central entities.
Requests separate short batch-local member IDs from their full topic labels.
The model returns those IDs; the compiler maps them back to exact labels and
rejects unknown, missing or duplicate members. Existing page names and titles
can guide identity reuse but cannot enlarge the batch's membership. Accepted
results from the precisely identified preceding functional planner can be resumed
only with identical inputs and catalogue dependencies and full current validation.

A complete model response can still omit or duplicate source IDs. These coverage
errors now trigger smaller batches instead of immediately ending the document.
An indivisible unit gets at most `max_attempts` validation attempts; persistent
failure remains unfinished. Invalid source quotations and malformed fact results
follow the same bounded recovery. No missing unit is silently accepted or skipped.
Logs include expected/received/missing/duplicate/unexpected counts, the requested
output limit and the provider finish reason, without recording document content.

Model quotations may replace Word's nonbreaking, figure or narrow nonbreaking
space with an ordinary space. Outside code blocks, a unique match differing only
by these one-character spaces resolves to the exact original spelling and
character offsets. This does not collapse whitespace, fold punctuation/digits,
accept neighboring text or choose an ambiguous match. Code quotations remain
strict. Fact failures identify the unit, block and invalid field without logging
source text. Completed batches from the preceding strict-quotation contract can
be reused only with their exact request identity and after current validation;
unrelated historical implementations and changed requests are not reused.

## Resume saved work

Continue the saved source after a stopped or unfinished task. With unchanged
source bytes, parsing settings and parser profile, the parser reuses its validated
saved result, including embedded document contents and image/OCR results. When
relevant cloud OCR work remains, Continue resumes accepted jobs and can submit
pages left unstarted by a previous budget. An explicit queue-full or rate-limit
rejection also permits a bounded new attempt; an uncertain submission does not.
Each continuation uses the current allowance, retains earlier attempts and reuses
completed OCR. Retained Word attachments participate in this recovery when needed.
Explicit reparse, changed source or parsing configuration can request new parsing work.

Compilation recovery has separate states:

- Individually valid fact rows survive a malformed sibling row. Restore matches
  the exact source/version/parse, model settings, endpoint and known executable
  contract, then validates quotes and coverage again. Different transport batch
  sizes do not require sending already validated units again.
- Saved split decisions lead directly to smaller batches; a known failed parent
  request is not resent simply to rediscover its split.
- Completed planning batches and verified generated parts are reusable with
  unchanged inputs and existing knowledge dependencies.
- A structurally valid generated draft is saved before semantic verification.
  Interruption during verification resumes from that draft. Correction state is
  also retained. A draft is never a verified publication receipt.

The currently unfinished request may need to be repeated if no complete usable
response was received. Each new operation starts a fresh execution allowance;
cumulative usage history remains visible. The request count and elapsed-time
limits above still apply. Reaching one does not start another task automatically.
Repeated semantic rejection remains pending; continuing does not authorize weaker
verification or an unbounded retry loop.

Empty extraction from a factual document cannot report successful compilation.
An empty body unit containing recognized numerical limits, requirements or
negations is retried instead of accepting an arbitrary `empty_reason`. This is a
conservative omission check, not proof of complete factual recall. Exact quotation,
source position, asset identity and title/body semantic checks remain in force.

Known local parse defects can retain usable content with an omission notice:
missing Markdown assets, an unclosed code fence, or a PDF page decoder failure.
Text decoding accepts UTF-8, BOM-marked UTF-16 and the existing GB18030 fallback.
Unreadable PDF pages retain their physical page position and an explicit unknown
content marker; this change does not infer their text. Storage errors, corrupt
checkpoints and unknown global quality failures are not treated as omissions.

Persistent factual or semantic defects still prevent the source's final atomic
knowledge publication. Other completed work remains checkpointed for continuation.
A stage percentage measures processed units/topics, not factual recall or whole
source publication. Desktop task details show the document's stopping reason and
recovery hint near the top, separately from worker cleanup status.

For providers that accept a `thinking.type` option, the optional top-level
`compilation_thinking` setting selects `enabled` or `disabled`. For example:

```yaml
model: deepseek/deepseek-v4-flash
compilation_thinking: disabled
verification_thinking: enabled
```

Set it in global or library YAML, or through the existing configuration REST
endpoints. `compilation_thinking` applies to extraction, planning, generation and,
unless separately set, verification. `verification_thinking` overrides only the
independent evidence review; it accepts the same two values. Navigation and chat
have separate model behavior. If neither mode is set, no override is sent and
the provider default is preserved. Library
overrides inherit global settings; a null REST patch removes the override.
Changing this setting invalidates affected compilation checkpoints and completion
profiles without changing the source or parse version. There is no automatic
switch of thinking mode when a budget is exhausted. Explicit non-thinking mode
is a provider option, not a guarantee of technical accuracy; see the
[real-provider validation record](document-provider-validation.md).

Before a contribution is published, an independent model request checks its
public title and cleaned body against the retained original passages and quotes.
The request preserves the roles of headings and neighboring blocks. Proposed
fact statements are planning aids; the original evidence remains authoritative.
Unsupported drafts receive at most one correction using the review feedback,
followed by another review. Uncertain, malformed or still-unsupported results
leave the entire source compilation unfinished. An earlier complete Wiki remains
available, and the retained original can still be read.

Link cleanup follows Markdown block and table-cell boundaries. Code examples
retain their literal links and original line endings; real image links must
resolve to retained source assets, with their titles preserved.

Generation, verification and correction share the same generation-stage and
document budgets. Batch planning includes all three complete request shapes,
using a lossless candidate and representative review feedback. Unexpected output
expansion is checked against the actual request limit and can still stop the
operation; no text is truncated to make it fit. Successful checkpoints bind the
review verdict to the final public title and body digest. The first accepted title
is fixed for subsequent parts; changing it later requires a new coherent proposal.
Changing only `verification_thinking` rechecks generation while retaining valid
facts. Model review can make mistakes, including false positives and false
negatives, so a completed result is not a proof of arbitrary technical accuracy.

Every task retains its own usage observations. Each source also keeps cumulative
LLM, local OCR and cloud job usage across explicit continuations. A continuation
starts a new bounded run; it does not erase earlier cost or resubmit a known
cloud job merely because observation stopped.

## Retained sources and reviewed publication

Intake commits immutable originals and related assets before parsing or model
work. A source has a persistent identity bound to its normalized origin; a
changed input creates a new version. Different origins remain independent even
when their bytes and parsing results are shared. Completed identical versions
can be skipped; unfinished versions remain actionable. Old evidence keeps its
source, input version, parse version, block and span.

Reliable PDF text is parsed locally, with physical page and available coordinates.
DOCX evidence uses headings, paragraphs and table cells, without invented Word
page numbers. Embedded document attachments are imported as separate retained
sources and included at their positions in the parent evidence. Supported document
extensions follow the normal import formats; scripts, executables, generic ZIP
archives and other non-document attachments are skipped, including those inside
document attachments. Generic archives are not recursively expanded.

DOCX pictures are retained. OCR is selective and advisory: small icons, narrow
toolbars (short edge at most 48 pixels), images whose long edge is below 160 pixels,
and almost uniform fills skip recognition. Other renderable images can receive OCR;
empty or unavailable recognition produces a warning, not a document-wide failure.
This size/contrast heuristic does not promise that every selected image has text
or that every character is recognized. Original missing image data is explicitly
marked. With usable body text, local DOCX omissions no longer block knowledge
compilation: unreadable document attachments, missing pictures or notes, malformed
optional comment/note parts and unsupported objects remain `needs_review` in the
immutable parse. The task retains their diagnostics and the published summary names
the omissions. A failed attachment retains its original document download and parent
position; later content continues normally. A wholly unreadable/empty document,
storage failure, cancellation or document deadline still stops processing. Historical
missing-image decisions remain bound to their exact source and positions; local
omissions are not relabeled as verified extraction.

Ordinary continuation reuses validated parse artifacts even if they contain local
warnings and even with automatic system/local OCR. It does not open the Word
container, unpack embedded OLE objects or run OCR again. Changed original bytes,
parsing/OCR configuration or parser profile select a different cache; an explicit
reparse bypasses the parent cache. Unchanged, already parsed child documents can
still reuse their own artifacts. A previously unreadable child without a parse is
attempted again during parent reparse. Installing/updating an OCR runtime under the
same configuration requires explicit reparse to refresh previously retained results.
These rules preserve source/parse identities and existing fact checkpoints.

Optional DOCX image recognition stops after its resource budget is exhausted or
credentials/quota prevent further work. This stop is shared with nested document
attachments within the current parse and resets for the next document operation.
Subsequent candidate images reuse available validated cloud results, or retain their
bytes and previews without new OCR calls; each document reports one count of skipped
frames. An empty individual image
or a failed image download does not prevent attempts on later images. PDF page OCR
remains independent because a scan may have no other readable text.

For PDF, bitmap and uncovered vector content can use the selected OCR backend.
OCR exceptions, timeouts and missing service-side assets are advisory when the original
page is retained. They do not block compilation of available document content; an
image-only page may have no known textual facts. The original visual remains available
for later inspection. Local native-content errors, unresolved source assets and
uncertain text/table extraction retain independent quality diagnostics. Under the
accepted publication policy, these ordinary issues do not block publication of
other usable content. Ruled table cells keep
headers and coordinates. With readable native text, full-page flat backgrounds,
simple thin rules at the page edge and small/flat raster icons can skip OCR while
retaining their visuals. Meaningful diagrams and image-only pages remain OCR candidates.
Successful supplemental OCR retains readable native text alongside the
original images and page preview; the cloud service need not return a duplicate
picture. Incomplete OCR preserves native text and keeps its explicit failure reason
as an advisory. OCR output is checked before its own successful checkpoint is
recorded. [Optional CPU deployment](optional-ocr-runtime.md) is separate from the
main application; local failure never selects the cloud backend automatically.
On Windows, the default local setting uses the installed Windows text recognition
engine and language packs without configuration. An explicitly configured local
PaddleOCR runtime or cloud backend takes precedence. Native Windows recognition
retains the rendered image and text positions; it does not infer diagram semantics.
An unavailable or timed-out Windows engine probe leaves native document parsing
available; cancellation and the overall document deadline still stop the operation.
Already validated cloud results can be read after the remote waiting budget expires;
task cancellation and the enclosing document deadline still apply to cache reads.

Source evidence binds figures to their paragraph, heading, physical page or attachment
position and neighboring text. The Q&A file reader exposes a source-image catalog with
the source line, adjacent text and validated wiki-relative image paths. Agents can
inspect those figures with `get_image` and embed them next to the relevant explanation
in an answer; unavailable OCR does not authorize guessing image text. Native chat renders
these images inline and fits them to the available width. An unsupported original image
format remains downloadable when a displayable preview is unavailable.
Publication resolves shared table-header figures through the same validated asset
catalog as body figures, including references from later cells. Each unique original
is copied once per publication; malformed Q&A image links do not prevent reading text.

A successful reparse task reports completion of parsing while leaving knowledge
compilation explicitly `not_started`. Existing task history and cited parse versions
are preserved.

Generation uses a private Wiki copy and preserves independent input bytes. All
knowledge changes for one source publish together only after parsing, version,
evidence and manual-change checks pass. Failure keeps the original and the prior
Wiki. Existing manual changes, including metadata, require review and explicit
acceptance of exactly the proposed differences. Changed inputs invalidate that
acceptance. External editors do not honor the application lock; the version
checks, short publication window and journal recovery reduce conflicts without
claiming an atomic whole-filesystem switch.

The desktop document list opens source status, bounded original evidence,
physical PDF page previews and proposed changes. Settings includes processing
budgets and separately retained local/cloud OCR profiles, with global inheritance
and per-library overrides. Enter the cloud OCR API key directly in
**Settings → Document recognition → Cloud configuration → Cloud API Key**.
Like the model key, it supports replacement, clearing the current override and
global inheritance. The password field never reads the saved key back.
The application stores it in the same private credential file as model keys;
users do not need to configure an environment variable. Existing custom
`credential_env` profiles remain a compatibility fallback when no directly saved
OCR key is available. A running task retains its captured key after rotation.

REST settings patches accept a top-level `ocr_api_key` string, separately from
`config.parsing`; omit it to retain the key or send `null` to clear the override.
Settings responses expose only `has_ocr_api_key`. The key never becomes part of
an OCR profile, parse identity, source manifest or model name.

The CLI `openkb source --help` lists source inspection, continuation, reparse,
page confirmation, page reprocessing and explicit history cleanup commands.
Mutating actions use the same task runtime as document import. Reprocessing one
page requires the current source and parse versions. A cloud submission with an
unknown outcome requires explicit acknowledgement before a new submission can
be made; the new job may incur duplicate cost. Known job IDs are queried before
any new work. Stopping local observation does not promise server cancellation.

History cleanup first produces a preview of unreferenced versions, parsing
artifacts and files. The cleanup action requires that exact preview identity;
state changes invalidate it. Current sources, referenced historical versions,
shared assets and OCR recovery records remain retained. Cleanup is never
performed automatically and does not erase cumulative usage history.

The `/api/v1/source` endpoints expose the same use cases. Source mutation
requests accept an input-bound `task_id` and return 202 with that identifier;
query and stop use the ordinary task endpoints. PageIndex Cloud application
features and old cloud-only formats have been removed. Local PageIndex remains
available; old cloud environment variables cannot route OpenKB to that service.

## Whole-document evidence compilation

Every nonempty structural block participates in fact extraction. Large blocks
and batches split according to the complete request and a representative JSON
output envelope. Spans keep their block identity and exact character positions;
table headers, row/cell locations, surrounding spans and hierarchical heading
evidence remain available. Each unit must return facts with verbatim quotes or
an explicit reason for having no facts. Missing coverage, invalid quotes and
truncated output leave the document unfinished.

The compiler merges topic plans across sections and generates concept/entity
contributions from reread original spans. Facts are a plan, not a replacement
for the original's conditions. Large topics use bounded parts within one source
contribution. The complete prior page is preserved outside model context; a
bounded relevant window and link catalog guide generation. Required figures
resolve to retained immutable assets. This source's previously generated
contribution is replaced, and retired topics lose only its marked contribution.
All changes remain private until the whole-source publication checks pass.

Validated fact, plan and generation responses become checkpoints. Keys include
source and parse identity, actual prompts/payloads, the model endpoint and
relevant compiler code. Planning and generation also include their Wiki inputs.
Changing the language can reuse independent facts; changing the source, model,
prompt or affected implementation invalidates the corresponding work. Progress
events expose checkpoint hits. Resuming starts a new bounded run while source
history retains earlier usage. A completed version is skipped only when its
compilation profile still matches.

## Rebuild source navigation

`navigation.enabled` defaults to true. Optional structure and summary enhancement
shares the import's bounded execution allowance and finishes before fact analysis.
Disabling enhancement still saves a complete basic PageIndex database index.

Each successful generation has an immutable database binding. Continue can reuse a
valid generation; rebuilding a damaged one creates a fresh document row. Queries bind
to the published index, so a new unpublished generation cannot replace their evidence.
The database, managed SDK inputs and binding updates participate in KB mutation recovery.
History cleanup removes unreferenced database rows and managed inputs together while
retaining indexes reached by current sources, citations, conversations and proposals.

Rebuild with `openkb --kb-dir KB source rebuild-navigation SOURCE_ID --version VERSION_ID
--parse PARSE_ID`, the desktop's **重建导航** action, or
`POST /api/v1/source/rebuild-navigation`. This uses the retained parse without
rerunning OCR or rewriting knowledge. `POST /api/v1/source/navigation` accepts
`kb`, `source_id`, `version_id`, `offset` and `limit` (1–200) for ordered navigation
windows. The desktop opens each position as original evidence.

## Maintained PageIndex distribution

This source tree includes a patched `pageindex==0.3.0.dev3+openkb.1` wheel under
`packaging/pageindex/`. Its manifest records the upstream release, commit, hashes
and MIT license. `openkb.patch` records the source changes; the upstream wheel and
its notices remain included. Rebuild with:

```sh
python scripts/build_pageindex.py
uv sync --frozen --python 3.12.13 --extra dev --extra api
```

For pip source installs, make the accompanying wheel available in the same command:

```sh
pip install --find-links packaging/pageindex -e ".[dev,api]"
```

Ship this wheel and manifest with any application wheel distribution. The local
version is not assumed to exist on PyPI. Source and desktop packaging include the
maintained sources and locked artifact. The patch fixes synchronous waits inside
PDF TOC processing, preserves cancellation through gathered results, exposes
caller-owned request execution, and closes model resources on the index's event
loop. It is rebuilt from checked-in upstream bytes, never by editing an installed
SDK in place.

## First-batch verification record

On 2026-09-09, CPython 3.12.13 was used on Debian 13 x64 and a Windows 11 x64
machine (build 26100.9445). These are synthetic model/control-flow results,
not OCR or semantic accuracy measurements:

- Debian compiler, application, REST, recompilation, diagnostic, desktop model
  and module-size regression selection: 352 passed.
- Debian review-fix, import-task, PDF fallback and converter selection: 48 passed.
- Debian auxiliary-warning and import-task selection: 20 passed, including
  actual slow-drip HTTP timeouts, POSIX CLI interrupt, forced teardown recovery,
  REST observation disconnect, historical reconnect and corrupt result handling.
- Windows shared-document, import-task, cancellation and PageIndex selection:
  54 passed, 1 POSIX signal test skipped.
- URL acquisition shares the document lease and elapsed budget with compilation;
  download/URL and task regressions passed on Debian (23) and Windows (22, with
  1 POSIX signal test skipped). Auxiliary/task checks also passed on Windows
  (18, with 2 POSIX-only tests skipped).
- Ruff and mypy passed (167 application modules). The application wheel and sdist
  built successfully; the sdist includes the patched SDK and processing guide.
  Rebuilding the PageIndex wheel on Windows and Debian produced identical bytes
  and the manifest's SHA-256, including under Windows Git newline conversion.
- Standards and Spec reviews reported no remaining findings after fixes.

No production budget, OCR throughput or content-coverage result is claimed by
these first-batch control-flow tests. The full suite is reserved for the final
three-batch integration check.

## Second-batch verification record

On 2026-09-10, the same Debian/Windows machines passed the retained-source,
quality, history, transaction and OCR adapter checks. The final Windows selection
passed 75 tests with one platform skip; subsequent OCR cache upgrade/recovery
checks passed 10 tests on both systems. Ruff, mypy (196 application modules) and
module-size checks passed. Standards and Spec reviews found no remaining
implementation findings after their reported defects were fixed.

Both systems completed the real optional CPU pipeline on one synthetic page
under the original 8 GiB/180-second limits. Shared document imports recovered
four prescribed facts through retained evidence, without calling a knowledge
model. Exact runtime, resource and offline-install results are in the
[optional runtime record](optional-ocr-runtime.md). Native settings and source
review/acceptance dialogs also completed through the actual local task runtime.

The fixed ten-document synthetic parsing corpus covers 100k/500k/1M-character
DOCX, 100/500/1000-page native PDF and 8/32/128/500-page mixed PDF. All Linux runs
finished within the fixed 30-second parsing bounds. Windows finished nine; the
500-page mixed sample exceeded its bound and its worker was reaped. Mixed pages
requiring OCR remain explicitly incomplete in these native-only measurements.
These checks do not establish semantic compilation quality or long OCR throughput.

Authenticated PaddleOCR and real-model synthetic checks were subsequently run;
their outcomes and unresolved semantic failures are in the
[provider validation record](document-provider-validation.md). Representative
user documents and broader OCR accuracy measurements remain outstanding. The
second-batch code checkpoint is not final acceptance of those external gates.

## Third-batch verification record

The same two systems exercised complete source compilation with a deterministic
model adapter through `scripts/benchmark_document_compilation.py`. Each case
extracts the manifest's three prescribed facts, stops before planning, then
continues from validated fact checkpoints. Every original block/span was
accounted for, all three facts were reread during generation and present in
published knowledge, and every worker was reaped. These are control-flow and
evidence-coverage measurements, not real model accuracy or throughput estimates.

| Fixed input | Blocks | Debian operation seconds | Windows operation seconds | Model-adapter attempts, Debian / Windows |
| --- | ---: | ---: | ---: | ---: |
| DOCX 100k characters | 92 | 2.44 | 4.97 | 157 / 157 |
| DOCX 500k characters | 384 | 10.86 | 23.70 | 741 / 741 |
| DOCX 1M characters | 750 | 21.44 | 42.70 | 1472 / 1473 |
| Native PDF 100 pages | 353 | 4.91 | 14.11 | 122 / 122 |
| Native PDF 500 pages | 1753 | 25.48 | 47.61 | 589 / 589 |
| Native PDF 1000 pages | 3503 | 50.13 | 129.61 | 1172 / 1172 |

The corpus manifest SHA-256 is
`a4cf2deb4bf1c097af01baf11948ef4a96426209f4bf0b3eae2c0f01fa47afa9`.
Per-case supervision allowed 180 seconds, 1 GiB worker memory and 512 MiB output;
per-run execution allowed 150 seconds, 120 seconds per stage, 4000 attempts and
10M reserved tokens. Requests used context 4096, output 1024 and concurrency 1.
No budget was enlarged after a failed run. Peak sampled worker memory stayed
below 273 MB. Other validation processes were active, so timings should not be
treated as isolated platform comparisons or production defaults.

Initial Windows probe isolation incorrectly blocked asyncio's own loopback
socket; the probe was corrected and rerun within the same bounds. The final
runner installs its external-network guard before third-party imports and uses
LiteLLM's bundled cost map. A separate 100k-character bootstrap check passed on
both systems (Linux also used an OS network namespace). Earlier corpus timings
do not prove network isolation during dependency import. All knowledge responses
were generated by the local deterministic adapter; no provider fee was incurred.

Regression coverage includes small output budgets, large Wiki catalogs, distant
heading prerequisites, DOCX table headers and row positions, long commands,
retained figures, entity vocabulary, invalid-output continuation, whole-source
manual review and navigation worker loss. Native Qt settings persistence and
source review → acceptance → independent navigation rebuild → original evidence
opening passed on both systems. The Windows renderer assets were rebuilt for the
correct platform and passed 11 rendering checks.

The final full-suite run collected 1550 tests: 1527 passed, 22 failed and one
platform test was skipped. The failures used superseded compiler fixtures,
two-call accounting or old URL/configuration contracts. After adapting those
tests to the shared operation, the affected 239-test selection passed. The full
suite was not repeated. A later malformed-navigation boundary regression and
the navigation/source API selection passed nine tests. The final Windows
CLI/REST/watch/recompile/URL/source-API regression passed all 244 tests in
154.35 seconds. The final module-size/desktop-source selection passed 12 tests.
Ruff, mypy (204 modules), wheel/sdist contents and both review axes were also
checked.

The subsequent [authenticated provider checks](document-provider-validation.md)
validated two-page PaddleOCR jobs on both systems and found real-model semantic
failures despite structurally complete generation. The three code batches do not
close that semantic gate, representative-document calibration or long OCR quality
acceptance. The provider checks do not establish a production processing profile.

### 任务进度

任务列表和详情显示可核对的阶段百分比：DOCX 按段落、PDF 按物理页、
文本按行、知识提取按已验证并保存的原文字符、知识页面生成按篇数计数。
父文档和嵌入附件保留各自的计数，详情可同时查看。缓存复用与跳过检查计入
已处理工作；这个比例表示当前阶段的工作量，不是成功率或剩余时间估计。

云端 OCR 和缓存完整性校验等无法确定总量的工作显示等待状态，不用耗时或
进程心跳填充百分比。失败、中断及部分完成保留独立状态，只有任务成功结束
才显示“任务完成 · 100%”。旧任务没有计数记录时显示“等待进度信息”，
新版本开始执行的任务会保存进度快照并通过任务 API 返回 `progress`。

### 来源片段与有界复核

同一知识主题可以包含正文不同任务和文档附件中的事实。生成请求明确列出来源范围，
每个原文窗口具有独立 occurrence ID（同一个事实被分为多个窗口时也保留各自身份）。
混合来源响应按片段返回对应关系；缺失、重复、未知编号及越范围分配不能进入发布。
程序组装的内容及片段对应关系一起交给语义核对。来源片段仍属于同一个知识页面，
中性公共标题和合理任务归类不构成自动拒绝理由。

原文完整窗口能放入请求时只测量一次预算；只有放不下才搜索切分边界。
生成时最多提供 64 个标题词相关链接候选，完整目录仍用于本地目标校验。
这两项减少本地准备和模型输入，不裁掉必需原文或前提、例外。

可在知识库 YAML 中显式启用失败时的深度复核（模型端点需支持该 thinking 选项）：

```yaml
compilation_thinking: disabled
verification_adjudication_thinking: enabled
```

这是知识库级高级设置，默认不启用，不改变事实提取、规划或正常生成的思考模式。
普通核对未通过时，同一候选最多增加一次指定思考模式的独立复核；复核仍拒绝则
继续有限纠正或报告未完成。不会循环重试直到通过。所有调用共用原请求、token、
时间和取消预算。普通核对与深度复核各自绑定实际请求和思考模式；改变深度复核设置
不会使未改变的事实、规划或初次生成失效。已有普通肯定结论无需额外复核，旧深度
复核记录不能冒充新模式下的结果。


有效的肯定或否定语义响应按完整请求持久化：候选、原文、提示、模型参数、连接和
重试位置共同决定身份。恢复时用当前规则重新验证记录；同一份已拒绝的候选不会
因为重新导入就再次抽签。格式无效或仍不确定的响应继续走已有的有界恢复路径。

DOCX 的代码可能分成多个普通段落。遇到独立结束括号时，生成阶段会在相同章节及
附件中回读最小完整配置片段，最多向前 32 段、8192 字符；引号或注释中的括号不参与
匹配，无法确定配对时不补造上下文。附加段落带精确原文引用，进入原有请求预算和
语义核对。并发工作从已验证的只读快照读取，避免在工作线程中访问持有写锁的知识库。


知识库还可显式设置 `correction_thinking: enabled`，使原有的一次纠正请求使用深度模式。
未设置时沿用正常生成模式；此选项不改变初次生成、事实提取、规划或语义核对。
它不会增加纠正轮数，仍使用相同输出、累计 token、时间和取消预算。失败稿的恢复记录
绑定实际纠正消息及模式，修改纠正策略后重新处理旧纠正稿；有效语义拒绝仍绑定原候选。


当有效的定位审查仅指出公共标题错误时，纠正只请求新标题。程序保留原正文、事实覆盖
和来源片段对应关系，只同步修改与旧公共标题完全匹配的开头标题，然后重新核对完整
候选。涉及正文或缺失覆盖的审查继续使用完整纠正，不能借标题修正绕过事实检查。

同一主题拆成多个请求时，子请求的校验另带从整主题原文选择的标题依据，只用于共同标题。
它不替代当前正文的原文证据或片段对应关系，也不能将不同任务变成前置依赖。
标题依据最多占可用输入预算的 1/12，且不超过 4096 tokens；保留完整上下文片段，
空间不足时可仅保留原文章节标题。整主题一次完成的请求不增加该字段。
首部分通过后，程序固定共同标题；后续模型提出改名时仍按固定标题核对其正文。
正文生成与完整纠正不接收这些其他片段的原文，避免将标题依据误写入当前正文。
只有程序保证正文不变的标题专用纠正可以读取它们。

可精确定位到候选正文或标题的无依据声明，允许其拒绝反馈没有来源 occurrence。
应用保留该否定结论；缺失覆盖与缺失来源路径仍要求引用，未知编号仍然无效。

知识库可分别设置 `compilation_reasoning_effort`、`verification_reasoning_effort`、
`verification_adjudication_reasoning_effort` 和 `correction_reasoning_effort`。
值为 `minimal`、`low`、`medium`、`high`、`xhigh`、`max` 或 `ultra`，实际支持范围由
供应商决定。未设置的核验继承编译选项，增强核验继承普通核验选项，纠正继承编译选项。
这些配置不改变默认策略，也不增加纠正轮数；实际发出的设置进入复用条件和费用记录。
较轻核验仍覆盖所有新候选，只有未通过且增强选项不同的候选才增加一次增强核验。

新生成的导航摘要也逐项独立核对原文。错误、含糊或协议无效的摘要退回原文预览，
不会成为事实依据。纯副本显示名称和随机导航措辞不再进入事实请求；版本、对象、
表头、顺序和必要上下文仍约束共享分析。每个来源保留自己的证据绑定。

费用按实际请求记录输入、缓存 hit/miss、总输出和供应商提供的思考明细，以及实际模型
名称和有效选项。输出已含思考时不重复相加，缺失明细保持未知。超时或断连后远端执行
状态不明时停止新增派发，不把未收到的用量补零，也不盲目重发。

若模型将 verdict/reason 与 issues 分别返回为两个 JSON 对象，仅在字段严格互不冲突、
第二个对象只有 issues 时无损合并，再执行完整校验。矛盾结论、额外文本和其他对象
不会被丢弃后当作成功。保存的原响应可直接重放恢复，无需再次请求同一判断。

删除资料时，多来源页面同时撤回该资料明确标记的正文区间及证据。删除依据同时核对
当前发布基线和该来源自己的发布收据；其他来源的一次链接清理不能把人工文字变成
可删除的生成内容。区间外正文、自定义元数据和无法确认归属的旧摘要保留。已修改、
缺失、交叉或嵌套的来源区间会使删除回滚；人工修改过的摘要显示为保留。
历史清理继续保护当前引用、已保存对话引用和仍被其他来源采用的分析记录。
人工改动的页面仍通过现有差异审阅和接受流程更新。更新时也核对该来源自己的
历史发布内容：接受其他来源的链接清理，不代表允许以后覆盖这里保留的人工内容。
摘要中可编辑的来源字段或正文标记被删除，不能绕过此检查；归属不明时要求审阅。

仅增加总请求数、总 token 或总运行时间后继续，同一原件中已完成且上下文未变的
事实单元可恢复，包括已确认不含独立事实的标题。索引的执行记录变化本身不使这些
结果失效；原文、必要上下文、节点含义或核验规则改变时仍重新验证。空结果不跨资料共享。

存在未解析内容时，发布前还要检查知识是否依赖缺失的前提、例外或表头。该检查保留
完整原文与候选内容，在允许的上下文范围内压缩重复引用。有效判定后，省略依赖缺失
内容或仍不确定的主题，发布其余独立主题；容量或协议问题有界拆分，最小候选仍无法
确认时跳过并保留诊断；没有可保留主题时仍完成来源登记及零知识摘要。请求、token 与时间上限仍然
生效，压缩本身不能证明知识独立或完整。

本轮实现的测试范围、真实实验成本和未通过项目见[来源索引验收记录](source-index-validation.md)。

依赖检查的返回若缺少候选、含未知路径或格式无效，单独保存原始返回及协议诊断，
有界拆分和重试后隔离无法确认的候选；继续可在原额度内补缺口。有效的依赖或未知
判定保留原理由并复用，不能通过重跑已拆分的父批次绕过子批次的拒绝。
请求超出显式最大上下文属于执行限制，不记录为模型拒绝。旧开发快照中带索引制品
ID 的空事实收据升级后可能重做一次；新版本内仅调整总请求、token 或时限不使这些
已完成单元失效。


## 2026-09-14：完成带缺失的导入与限定范围的回答修正

损坏的 PDF/Office 包、明确的文本解码失败及已记录的 OCR/附件问题保留原件，
通过正常事务发布来源和缺失说明。全部知识被排除时不调用额外模型编造摘要事实，
显示生成知识 0 条。CLI 返回成功，REST 和桌面将发布完成与 partial 覆盖分别呈现。
声明为已保存的原件或资产损坏、输入身份变化、硬额度与事务恢复故障仍阻止执行。

人工修改后的原页与自动生成基线分别保存。相同候选的 Continue 不覆盖人工补写，
也不把人工文字重新认领为自动生成；真正的新差异继续走既有人工覆盖保护。
历史清理同时保留待接受提案引用的原件，以及归属比较所必需的旧生成字节。

原文读取工具提供 `short_citation` 标识。程序只根据已观察的来源工具结果展开为
完整的版本、解析和块链接；未知/歧义标识不猜测匹配，原有合法完整链接继续有效。
Markdown 代码示例及未改动排版保持原字节；答案核验、最终显示和会话保存使用同一
渲染结果。终端逐步显示工具进度，完成核验后显示最终答案，避免留下内部短标识。

回答核验保留明确的受影响单元 ID，包括跨单元问题；相同词句不自动扩大修改范围。
修正协议只允许替换已核验定位的单元，或为明确缺失插入内容，再核验组装后的全文。
修正后再遇到坏引用仍沿用原允许范围，不能退回整篇改写。无效核验只允许对原答案
重核一次，不开放全部单元。所有修正和核验仍共用原请求与 tokens/时间额度；
回答失败不阻止或回滚已经完成的资料导入。
