# Document execution

Imports use one isolated task runtime in the CLI, REST API and desktop. A task
captures settings before document processing begins and keeps that configuration
for the rest of its batch. Updating settings applies to a new task.

## Results and stopping

Each document reports `source_intake`, `knowledge_compilation`, `stage`, `reason`,
`quality`, `unfinished`, `resources`, `warnings` and `usage`. A completed or
previously completed identical document is successful. Invalid plans, partial
model output and exhausted budgets cannot publish knowledge. An ordinary failed
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

## Default execution profile

New and existing knowledge bases automatically inherit finite processing budgets;
no manual setup or configuration migration is required. The desktop's
**Settings → Processing budgets** shows the effective values. A complete
`processing` mapping in global configuration overrides the built-in profile,
and a knowledge-base mapping overrides the global profile. Clearing an override
restores inheritance. Reading settings does not write defaults into a library.

| Field | Default | Meaning |
| --- | --- | --- |
| `context_tokens` | 32768 | Total context cap for one complete request |
| `output_tokens` | 8192 | Output reserve within that context cap |
| `request_timeout` | 180 | Seconds per request without an explicit timeout |
| `stage_timeout` | 1800 | Seconds allowed for one processing stage |
| `document_timeout` | 3600 | Seconds for the whole document operation |
| `cleanup_timeout` | 10 | Seconds for auxiliary cleanup or shutdown grace |
| `max_attempts` | 2 | Maximum attempts for one logical request |
| `max_requests` | 200 | Maximum observable model attempts across the document |
| `max_tokens` | 2000000 | Document token budget, including outstanding reservations |
| `concurrency` | 2 | Maximum concurrent model calls |

These are bounded starting allowances, not measured model capacities or a
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
retries only transient transport/service errors within the configured limits.
Observable attempts and unknown internal transport attempts are distinct fields.

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
marked; a recorded decision to proceed applies only to the exact source/parse and
missing-image reasons. Reprocessing the same source version carries that decision
forward only when the missing markers have identical physical positions and counts,
including their attachment identities. It records a new exact decision and preserves
the old evidence. Changed original bytes, new missing locations and other content
failures are not waived. Harmless Word
formatting/style conversion warnings are advisory; unsupported document content
still requires review.

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
for later inspection. Corrupt native content, unresolved source assets and uncertain
text/table extraction remain independent source-quality checks. Ruled table cells keep
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

## Optional original navigation

`navigation.enabled` defaults to false. Basic ordered positions remain readable
after compilation. To enable the pinned local PageIndex enhancement, set
`navigation.enabled: true` and provide a separate `navigation.processing` mapping
using the execution fields above. These limits require explicit calibration;
they do not borrow the document's compilation allowance.

Knowledge publication, settled usage history and the runtime completion receipt
precede navigation work. Navigation failure or stopping reports a warning and
retains basic positions. Each navigation attempt records reservations and settled
usage separately; an interrupted attempt keeps unknown usage visible.

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
