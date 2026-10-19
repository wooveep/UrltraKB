# UltraKB native desktop guide

The desktop opens local knowledge bases in the current UltraKB format. It uses Qt
Widgets for its interface and local static rendering for mathematics and Mermaid.
The CLI and independent REST API use the same application operations and data.
The desktop defines the common business behavior; terminal output, HTTP responses
and desktop task presentation adapt the results to each interface.

Task details keep confirmed item progress separate from the model-call budget.
The current item shows running, waiting for the KB, or stopping as appropriate;
later items remain queued unless stopping has been requested. A model budget
shows sends used, its limit and remaining capacity without a completion ratio.
The processed-item bar includes confirmed failures and skips and does not estimate
the percentage of compilation within one item. See
[default capacities and execution limits](default-limits.md) for their scopes.

For this branch's baseline, selected desktop changes and verification results,
see [dev-1.2.0](dev-1.2.0.md). Historical package acceptance below belongs to its
recorded source revisions; this branch has separate source-level validation.

## Launch and data locations

On Windows 11 x86_64, unpack the complete program directory and run `UltraKB.exe`.
On Debian 13.6 x86_64 GNOME/X11, extract the program archive and run `UltraKB`.
Runtime archives and their top-level directory are named **UltraKB**. Full
source/build materials are a separate `UltraKB-VERSION-materials.zip` download;
licenses and the companion archive checksum remain available in About.
Keep the program's resources together. No developer Python, Node or Rust runtime
is needed to run a complete portable build. Other Linux desktops, Wayland,
macOS and ARM have not been accepted as distribution targets.

Use the knowledge-base manager to create a new KB or open an existing one.
Registered locations and locations beneath the configured KB root appear with
their full paths. Two KBs may share a directory name; select by path. Switching
the active KB changes what you browse and where new work is submitted. It does
not stop work already submitted for another KB.

Knowledge-base configuration remains in `.ultrakb/config.yaml`, KB credentials in
its `.env`. Conversations, tasks, source versions and publications share
`.ultrakb/kb.sqlite`; immutable content is retained in `.ultrakb/blobs`. Global configuration
uses `~/.config/ultrakb/`. Older data requires the
[name-switch procedure](../README.md#名称切换与旧数据). Task summaries and
directory lifecycle records also live outside the program directory. Replacing
program files after a full exit does not move or delete these data.

## Workbench navigation and appearance

The compact top bar selects the active KB; hover the selection to inspect its
full location. **管理** opens knowledge-base administration. The application menu
(**⋯**) also provides Create, Open, diagnosis without opening a damaged KB, About
and explicit Quit. Opening a KB starts on **概览** (Overview), with existing
statistics, recent compilation/inspection times and shortcuts.

The left navigation contains **概览、资料、知识、对话、产物、任务**, with **设置** at the
bottom. The navigation toggle switches between text labels and an icon rail.
Names remain available as tooltips and keyboard focus is visible. A narrow
window temporarily compacts navigation; widening restores your saved preference.

**知识** contains reading and body editing. **知识目录** and **来源与链接** toggle
secondary panes without discarding the selected page or draft. Narrow windows
initially hide the directory. Sources and links always belong to the current
page; task outputs belong to their task on **任务**, including tasks from another KB.

**设置 → 外观** offers **跟随系统** (default), **浅色**, and **深色**. The selected
mode applies to all pages and newly opened dialogs. Appearance and sidebar
choices use the Qt `UltraKB/UltraKB` application identity in the user's
platform settings, separately from KB/model/credential configuration. The reader's
zoom is available on the Knowledge page and applies to content previews as well.

New knowledge bases dynamically inherit the global model connection and defaults.
**设置 → 所有知识库** changes the global defaults; **当前知识库** shows each effective
value and its origin. Choose **设置本库覆盖** for an independent value, or **恢复继承**
to remove it. Existing explicit overrides remain in place. Credentials resolve
from the KB, then the startup environment, then global settings. The settings page
explains when startup credentials take precedence over a saved global value;
API keys are never read back. Ordinary CLI and HTTP creation also inherit defaults.

The connection section offers a few documented service examples and keeps manual
model/address entry. **高级参数** reveals capacity, recovery, PDF and Office controls.
Selecting the OpenAI or DeepSeek example fills a sourced model capacity profile
for its exact model and official endpoint. Model context and output limits are
separate from an optional application byte guard. Full requests share one input
measurement and admission path; the actual requested output is reserved from the
context. A different manually entered model or private endpoint needs its own
capacity declaration. The retired 64 KiB factory preset migrates under settings
locks with a before/after receipt when a new operation captures its configuration;
explicit user limits and historical snapshots remain intact. See
[capacity and recovery rules](default-limits.md#模型上下文与输出).
**保存设置** saves independently of testing. **测试并继续** first saves pending choices,
then uses small synthetic input to check text and one function-call round trip,
with a request limit, a 20-second deadline and **取消测试**. It sends no KB content.
A pass shows its time and capability scope and can be reused by inheriting KBs;
changing the effective connection invalidates that pass. Opening settings does
not probe the service. A failure explains how to recover, keeps the saved choices
and allows local browsing. CLI and HTTP operations keep their existing entry flow.

See [implementation and evidence](desktop-workbench.md) for the scope, assets,
actual application screenshots and reproducible source acceptance commands.

An empty KB can be created and browsed before configuring a model. The first
model operation opens a connection guide with global defaults and explicit KB
overrides. **稍后继续** preserves the empty KB, saved settings and original input;
**概览** offers a continuation button. A successful test resumes the captured
operation once, with its original target KB. Switching KBs pauses this continuation;
return to that KB to resume. Closing the originating panel prevents its operation
from submitting. The worker checks the effective connection again after acquiring
the KB lease, so a changed connection cannot reuse an earlier verification.

## Import and maintain knowledge

The import area keeps the submitted KB path, progress and **查看导入任务详情** visible.
It shows completed/total items, the current file and its readable processing stage.
The item counter advances for confirmed outcomes; it does not invent a percentage
for an individual conversion or model call. Committed items trigger a list refresh
between files. If a writer still owns the KB, existing rows stay visible with a
waiting notice. Open task details continue updating without losing your scroll position.
After usable content is available, **开始提问** opens the chat. If you switched KBs,
**切回导入目标并提问** explicitly returns to the original target. Completion updates
the notice without changing your page or current KB. A partial result can use
available content while task details explain unfinished work. With no usable
content, the notice directs you to task details and the document's recovery menu.

Use **导入资料** on **资料** (Documents) for files; its menu also accepts folders
and URLs. **导入选项** folds the less frequent image-download override. Search by
name or filter by reading/processing state. Each row offers **阅读** and **更多**;
the latter includes details, retry, regeneration, reprocessing and reviewed removal.
Unavailable commands explain their reason on selection and in the menu. Technical
processing fields appear in details. **知识维护** on Knowledge provides whole-KB
regeneration, updates and proposal review; **任务 → 待处理工作** opens durable jobs.
Supported document inputs
include PDF, Markdown, Word, PowerPoint, Excel, HTML, text, CSV, CAJ and KDH. Each task
shows its KB, current stage and individual outcomes. An already indexed input
may be skipped. A batch can retain completed documents while reporting failed
or unprocessed ones.

Checkboxes select the explicit batch independently of the focused row. **全选当前结果**
and Ctrl+A while the table has focus select only the filtered rows; shortcuts inside
search stay in that field. Changing a filter clears checked items with a notice.
Sorting and refresh retain typed identities; disappeared documents leave the selection.
The batch bar shows selected/applicable counts and skip reasons. **重新生成知识**
rechecks and confirms the exact applicable subset before submission; row menus say
**仅此资料** and preserve the checkbox set. **重试未完成项** reviews unfinished
source/worksheet targets and excludes completed ones. Independent local failures
allow the next item; model authentication, permission, missing-model and quota
faults stop that batch's remaining work. Stopping waits for active cleanup and
preserves commits. Task details distinguish exclusions, attempted failures,
unconfirmed outcomes and unstarted items. A batch retains its first captured
settings; later changes affect new operations.

When a Source or worksheet has an unfinished failed summary, the row and batch
**重试未完成项** confirmation offers **重试失败摘要** (unchecked by default).
Selecting it permits the existing one-time recovery of that failed response;
the operation reuses its retained index and accepted summary batches, then continues
unfinished compilation. Source revisions and the latest failed publication are
checked again before execution. Recovery retains the original model-task budget
and does not replenish automatic repair attempts.

On summary continuation, a captured response for the exact retained request is
checked locally before any new send. Its owner, stage, protocol, section IDs and
body checksum must agree, and it must pass the current source, section and numeric
validators. A passing response becomes an accepted checkpoint with its origin and
source-copy receipt retained; existing repair flags and model-call usage remain.
Truncated output is never recovered this way, and an invalid saved response does
not grant another repair attempt. Only still-missing batches need model sends.

Invalid compiler responses are retained verbatim as immutable Blobs. Main-store
records link their execution, stage, protocol, expected section IDs, request digest
and validation error. Task diagnostics expose their retained references; a reference
is not a path to edit. Accepted computations and paid responses survive publication
or working-copy synchronization failures. Explicit diagnostic archival preserves
responses still needed by recovery and accepted work.

Summary prompt examples, local validation and response schemas share
`ultrakb/agent/summary_protocol.py`. Known hosted models use strict JSON Schema when
the pinned model catalog declares support; unknown models and custom endpoints
retain JSON mode plus strict local validation. Official `deepseek/deepseek-flash`
summary requests use the pinned SDK's Responses bridge with `text.format` JSON
Schema, as documented by the [DeepSeek Responses API](https://api-docs.deepseek.com/zh-cn/api/create-response/).
DeepSeek Chat Completions only supports `json_object`; its model-table schema flag
does not enable an unsupported Chat Completions wire format. Other compilation and
indexing requests retain their existing transport. Capability selection makes no
probe request and does not retry with another format after a provider rejection.
Native checkpoints retain their exact prompt identity and consumed repair opportunities.
Accepted batches remain reusable. Remaining sends use the available strict schema after the normal final
capacity check; an oversized upgraded request stops without discarding old results
or spending a model send.
Native Responses terminal status is retained at the HTTP boundary: incomplete
output follows the existing truncation repair, while failed or cancelled output
cannot become a successful summary even if the SDK bridge labels it `stop`.
New checkpoints record the delivery-contract generation; strict-schema overhead is
included in short-summary preparation and long-summary input admission. Neither
path replenishes the task budget. Schema construction follows the supported subset
in the [OpenAI Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=chat).

Validation diagnostics include field paths, expected shapes and actual types.
Repair requests use a finite set of program-owned field hints, without echoing
model-controlled field names, values or raw exception text. Their admission reserves
all possible hints before generation. A schema-valid response must still pass the
existing section coverage, original excerpt and numeric checks.

New long-document summary checkpoints use structured `parts`: `prose` explains
facts and conditions, `excerpt` selects supplied reference IDs, and
`derived_template` declares a path inference with its concrete source references.
The program copies excerpt characters from the frozen source windows. IDs retain
the selected occurrence, including repeated examples, across checkpoint recovery.
Earlier checkpoints keep their original prompt, window layout and accepted payloads;
recovery does not silently migrate them to the new reference protocol.

For retained free-text responses, excerpt validation tries exact matching first,
then uses the same Unicode whitespace rule on both sides to locate a continuous
source range and copy the original characters back. It still protects quoted
values, token boundaries, line structure, control-flow indentation and complete
tables/examples. Native PDF/DOCX NBSP transcription and JSON member indentation
can be restored from their original positions; ambiguous occurrences require an
explicit reference. Physical pages, printed page labels, copyright text and
surrounding context ranges remain separate source metadata. Trimming recognized
page furniture never removes the retained original.

A derived path is labelled as a template and must preserve every fixed path byte.
The current validator supports UUID placeholders such as `{domainId}` and needs
two distinct concrete paths from the selected original excerpts. Unsupported
inferences fail with a diagnostic rather than being accepted on their label alone.
Failure sidecars include the response field and line, source window/range and
Unicode codepoint differences where a candidate can be located. Successful
source-copy receipts retain source hashes, positions and applied differences in
the same diagnostic directory. Approximate candidates are labelled as candidates;
an unrelated paragraph is not reported as the quotation's source. Structured
summary requests without their original windows are rejected before any send.

Saved responses and accepted summary batches are retained in the native main store.
`pytest tests/test_retained_summary_reuse.py tests/test_summary_budget_recovery.py
 tests/test_storage_summary_retry.py` checks exact request binding, recovery without
resending accepted work and explicit consent before one additional failed-summary
repair. It uses real SDK requests against local HTTP fixtures.

**移除所选资料** reviews the aggregate effect of withdrawing new Sources, including
pages that will lose all effective sources. Originals, images and historical
snapshots remain; affected knowledge shows pending refresh and withdrawn sources
are excluded from current answer evidence. The confirmed version is rechecked under
the KB write lease, and each Source has its own transaction. Stop or failure retains
completed withdrawals and reports unexecuted items. This operation makes no model
calls and never deletes external originals. Mixed batches show separate Source
withdrawal and legacy cleanup consequences and require both group confirmations.
Only legacy documents offer **保留原始文件副本** and **保留失去来源的空页面**;
changing an option recalculates the whole preview and clears confirmation.
Shared legacy files and pages are aggregated across the selection, while referenced
images and immutable Source history stay protected. Task details record committed
cleanup, failed phases and unexecuted items. Legacy cleanup can make old links
unreadable; the UI makes no migration or undo promise.

Local CAJ/KDH files retain their original name and bytes. Supported CAJ, KDH
and embedded PDF content becomes an internal PDF for the ordinary import
pipeline. HN, C8, TEB and unknown content are reported as unsupported. Source
details show conversion diagnostics and offer the internal PDF preview. A
converter policy change requires explicit reprocessing; retries and historical
reads reuse validated retained artifacts.

The document list supports removal and recompilation. Review the preview before
confirming changes. Recompilation can rewrite knowledge pages; open editor drafts
are checked against the current saved version before a later save. Structural
and semantic maintenance report their results separately. A model-generated
output may have quality issues even when a file was successfully produced.

If recovery cannot restore a safe state, ordinary operations remain blocked for
that KB. Use the explicit repair action and inspect its result. Restarting or
retrying a failed task does not bypass the repair requirement.

Deleting a KB requires its full location and typed name confirmation. The
desktop stops matching subscriptions and tasks before deleting its directory.
A partial deletion stays visible for an explicit retry. It cannot be resumed
against a replacement directory that happens to occupy the same path.

## Read, edit and follow links

Browse summaries, concepts, entities and explorations in **知识** (Knowledge). The page context shows
source material, outgoing links and backlinks. Missing or ambiguous targets are
reported rather than linked to an arbitrary page. Code blocks, tables, images,
Chinese text, mathematics and the supported Mermaid families display natively.
Single-click a directory entry to read it. Images resize with the reading pane;
switching themes preserves the reading position and unsaved drafts.
Reading and focused questions immediately show their target or a waiting notice
when import/compilation owns the KB. Leaving that workspace cancels the pending
jump. Returning restores the previous page and its directory selection; a failed
read can be retried by clicking the same title.

**我的知识** groups pages as **资料摘要、主题与概念、人物与事物、探索笔记**.
Search titles and descriptions or sort by recent updates and title. Page headings
replace storage filenames in the directory. Internal instructions, index, log,
raw sources and inspection reports are excluded; source reading stays in
**资料**, and reports remain accessible from **产物** and maintenance results.
**围绕此页提问** opens a new conversation with a reference to the current page;
edit the proposed question before sending it.

In **资料**, the inventory distinguishes **Markdown 全文编译** from **PageIndex 长文索引**.
Select one document and choose **阅读原文** to open the converted text or retained
PDF page text. Ordinary imports and PDF imports below the configured threshold
use Markdown. Long PDFs build a PageIndex tree before compiling knowledge.

The editor saves through the shared page operations and preserves metadata.
If another entry point or an external editor changed the page after it was
loaded, saving reports a conflict. Reload and reconcile the draft instead of
overwriting the newer file. Saving while continuing to type retains the newer
unsaved draft.

Formula and diagram failures leave a readable source fallback with a diagnostic.
Unsupported syntax is not silently converted into a successful diagram. HTML
inside Markdown is not a browser application. The accepted rendering corpus and
its checks are described in the [build guide](../packaging/desktop/README.md).

## Ask questions and keep conversations

**对话** (Conversations) opens directly into an automatically saved, multi-turn chat.
Type a message and press Enter; Shift + Enter adds a line. **新对话** starts a fresh
context and **对话历史** opens saved chats with one click. Reopening a KB restores
the last selected conversation. The complete transcript stays visible during a
follow-up, and the composer offers **停止** while a reply is running.

The chat streams response text into the current card with a progressive typing
effect, retaining previous message cards and the reader's scroll position.
Task status and elapsed time appear separately. A tool call clears that request's
intermediate narration; the completed answer replaces any provisional text.
It uses Wiki tools and the original long-document retrieval. Explicitly tagged
reasoning stays hidden, including tags split across chunks. Quoted
code, citations, formulas and diagrams remain available. Completed turns retain
the established session model/language and reusable SDK history. Accepted submissions are first saved in a private desktop outbox, including
while the KB is busy. Recovery transfers them into the conversation without
replaying model requests; interrupted submissions remain in the timeline
with an unfinished notice and are never counted as completed model turns.

Messages and the composer stretch with the workspace; the assistant answer has no
surrounding card border and user questions use a light background. Wide tables and
code scroll inside their reader, while figures and display formulas fit its width.
Other knowledge readers keep their existing reading width. The input has no footer
hint or cumulative usage counter. Activity and unfinished notices sit beside the
corresponding answer. Enter sends, Shift+Enter adds a line, and confirming an input
method composition keeps that action inside the composer. Reading earlier messages
preserves scroll position during streaming; switching chats preserves drafts.

Each committed answer has keyboard-accessible **复制** and **字符** actions below
its body. Copy uses that answer's retained Markdown and existing citations, with
success or retry feedback. Character counts use the actual rendered text, including
punctuation, code and tables, excluding whitespace and Markdown formatting. Combining
characters and emoji clusters count as one; flag pairs follow
[Unicode UAX #29](https://www.unicode.org/reports/tr29/#Grapheme_Cluster_Boundary_Rules).
Graphical formulas and images count as zero text characters; visible source fallback
or alternative text counts when it is actually displayed. Details show only reported
usage belonging to that exchange. Older records without it say **未提供单条模型用量**;
cumulative conversation usage is never substituted. Streaming and unfinished notices
have no completed-answer statistics, and statistics make no model requests.

Choose **提问** on a document row to prefer that document in the current KB.
The chat header names the KB and focused documents, with **取消关注** and a
recovery entrance. This preference still allows retrieval from other relevant
documents in the same KB. Partially processed workbooks can use completed sheets;
failed and pending sheets remain explicit. Documents without usable content offer
reading, reprocessing or retry from their row's **更多** menu.

**出处（N）** below each completed answer expands references actually present in
its Markdown, deduplicated by Source identity or legacy registry identity. A focus
label or tool activity alone never creates an answer citation. Original-reading
receipts distinguish references with returned evidence from links alone. Body
links and **核对原文** open the captured version; **返回回答** restores the answer's
scroll position. Missing or changed retained evidence shows a recovery message
instead of silently substituting another revision. Older answers without snapshots
say so and can only offer current readable references, without claiming historical
provenance. Saved summaries and knowledge pages remain identified as generated
content rather than originals.

Code fences use selectable syntax colors in both themes. The delimiter's final
line ending does not create a blank code line; intentional internal blank lines
are preserved. Formulas and diagrams receive their full rendering when the reply
finishes.

History's **更多** menu exports a Markdown copy or confirms deletion. Exports use
new filenames to preserve prior copies. CLI/API one-shot queries remain available.

## Generate and export outputs

**产物 → 创作工作台** follows a brief-to-result workflow. Choose **新建创作**, select
a reusable Skill or HTML presentation, name it and describe its audience and
purpose. Generation continues in the background; **查看任务进度** opens the task
page. The brief remains available for another iteration. **生成知识图谱** is a
separate action for exploring existing knowledge relationships.

**我的成果** supports name search and type filters. Selecting a result opens its
primary guide or preview instructions. Supporting files, source and task records
remain available as secondary views. **导出完整成果** exports the entire artifact
bundle; a browser preview opens the chosen HTML file. Internal generation workspace
directories are excluded from this result library.
An existing Skill or deck name requires a new name or explicit consent to archive
and replace its output. Graph generation retains its established fixed path.
CLI generation uses `--yes` for replacement. REST `/api/v1/skill` and
`/api/v1/deck` default to refusing replacement (HTTP 409, or an SSE error with
code 409); send `replace: true` to archive and replace. An optional `version`
binds the request to a previously reviewed target. Skill ZIP downloads use the
same validated file set and top-level artifact directory as desktop exports.

Opening an HTML deck or graph in the default external browser is an explicit
preview action. The workbench itself does not embed a browser. Advanced Skill
evaluation, validation, history and rollback remain available in the CLI.

## Settings and task outcomes

Global and KB settings expose the model, language, long-PDF page threshold,
entity types and API credentials. Advanced provider options remain available in
the configuration file. Secret fields distinguish keeping, replacing and
clearing a value. Shared business operations in all three entrypoints prefer KB
credentials, then the launch environment, then global settings. Desktop setup
uses API keys; subscription login
continues to be a CLI capability.

A queued task uses settings resolved when business execution first begins,
after obtaining access to the KB. A running task keeps that configuration;
an entire batch shares its initial configuration across its documents. A new
watch-triggered task or manual retry captures its own settings.

The top bar shows running counts and results needing attention without changing
your current page. Click it to open **任务**, where the task list distinguishes
waiting, processing, stopping and terminal results.
Inspect completed, skipped, failed and unprocessed items as well as retained
outputs and quality diagnostics. Stopping prevents later units from starting
and lets required recovery/commit work finish safely. It does not undo earlier
completed work. A worker exit without a reliable result is shown as interrupted
or unconfirmed, not inferred to be a success.

Manual retry previews what remains and creates a new task. Failed work is not
automatically replayed. Clearing task history removes summaries, not generated
artifacts or conversation records.

The **任务** page searches by task, file or KB and filters by status or the current
KB. Row focus opens that task's details and retained outputs; checkboxes define a
separate batch. **全选当前结果** and table Ctrl+A select the filtered set. Filtering
clears checks with a notice, while polling and sorting preserve stable task IDs.
Each row's **详情 / 停止 / 更多** operates only on that task.

The batch bar supports **安全停止所选任务**, **重试所选未完成任务** and
**清理所选任务摘要**. Mixed selections show applicable counts and per-task skip
reasons. Retry first reviews receipts and retained outputs, then creates an
independent task for each approved original's unfinished units. It rechecks
versions and the target KB's actual connection, including explicitly selected
KBs other than the one currently open. Canceling configuration preserves the
review; checking again cancels unsubmitted continuations. Closing the review
prevents later tasks from being submitted.

Summary cleanup confirms the exact ended subset, retains all KB content and
artifacts, and refuses tasks whose results a watcher still references. Running
or already-removed tasks are skipped with reasons. Stop preserves committed
results and waits for cooperative worker cleanup. The task table, import area
and open details use the same current-stage presentation; earlier item failures
remain in outcomes rather than replacing the stage of the next active item.

## Watching and exit

In **任务 → 目录监听**, enable directory watching explicitly for a KB's `raw/` directory. The native
watcher first checks for files that have not been imported or have changed,
then coalesces changes and waits for stable input. Atomic file replacements and
nested directories are handled. Removing a raw file does not automatically
delete its compiled knowledge.

Stopping a subscription stops reception of new changes. Tasks it has already
submitted remain in the task list and can be stopped separately. CLI and REST
watch retain their existing startup behavior; they do not inherit the native
startup scan. All three entries share KB locking and input validation.

Closing the main window leaves it in the system tray when a usable tray is
available. Use the tray to reopen it. Without a usable tray, the window remains
visible. Explicit Quit stops accepting new work and offers waiting for submitted
work or stopping at safe boundaries. It waits for workers, observers and renderers
to exit. A fresh launch does not restart tasks or subscriptions automatically.

For a manual update, finish explicit Quit, replace the complete program directory
with the new version, then reopen your existing KB locations. Do not replace
program files while background work is still running.

## Command locations after the desktop update

| Task | Current entrance |
| --- | --- |
| Import files, folders or URLs | 资料 → 导入资料 menu |
| Reading and focused questions | Document row → 阅读 / 提问 |
| Details, reprocessing and single-document maintenance | Document row → 更多 |
| Retry or remove an explicit group | Check rows → 重试未完成项 / 移除所选资料 |
| Recompile the explicit group | Check rows → 重新生成知识 |
| Whole-KB maintenance and knowledge refresh | 知识 → maintenance menu / 检查与修复 |
| Task results, stop and retry | 任务, or 查看导入任务详情 for the captured import |
| Directory watching | 任务 → 目录监听 |
| Model connection and advanced defaults | 设置 → global or current KB tab |
| Answer copy, characters and evidence | Below that answer → 复制 / 字符 / 出处 |

New concept and entity pages plan complete requests before concurrent generation.
Large original selections are partitioned with exact contiguous coverage checks;
duplicate machine metadata is projected out while original text, table facts,
captions and partial-coverage warnings remain. Each accepted contribution is
checkpointed independently. Retrying reuses accepted contributions, document
summaries, the accepted page plan and the original model-task call budget.
Contributions are assembled locally; a final oversized rewrite is not required.

Settings distinguish a local UTF-8 byte guard from a model token input limit and
output reserve. Legacy `utf8-bound` settings keep their saved values and processing
identities. New byte-only profiles use `max_input_bytes`. The full limit reference
is [default-limits.md](default-limits.md).

`pytest tests/test_create_capacity.py tests/test_create_assembly.py
 tests/test_import_capacity_recovery.py tests/test_index_capacity_recovery.py`
checks capacity, interruption and continuation against native storage and actual SDK
requests to local HTTP services. These checks retain originals and ready indexes
without publishing test results to an existing knowledge base.

The source acceptance matrix for this update is
[A01–A24](validation/desktop-ux-A01-A24-2026-10-15.md). It distinguishes native
desktop interaction, offline tests and real-service runs, including platform
limits and retained evidence.

## Version, source and licenses

Open **Application menu (⋯) → 关于 UltraKB · 源码与许可** to inspect and copy the
installed version, source commit, material filenames and SHA256 checksums. The
license tab displays original copyright and license texts without opening a
browser. The material-directory button locates the matching source archives,
component inventory and build records supplied with a complete distribution.

The independent REST API advertises `/api/v1/distribution` in its `Link` response
header. This public endpoint lists only fixed release materials; its download
routes remain available when private KB endpoints require authentication. Files
are verified against the manifest and served from fixed temporary copies.

Development environments without matching installed build identity and release
materials are identified explicitly. A missing, damaged or mismatched manifest
does not become a verified release by pointing to the public repository. Final
distribution acceptance remains subject to the [build guide](../packaging/desktop/README.md).
