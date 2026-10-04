# UrltraKB native desktop guide

The desktop opens local knowledge bases in their existing format. It uses Qt
Widgets for its interface and local static rendering for mathematics and Mermaid.
The CLI and independent REST API use the same application operations and data.
The desktop defines the common business behavior; terminal output, HTTP responses
and desktop task presentation adapt the results to each interface.

For this branch's baseline, selected desktop changes and verification results,
see [dev-1.2.0](dev-1.2.0.md). Historical package acceptance below belongs to its
recorded source revisions; this branch has separate source-level validation.

## Launch and data locations

On Windows 11 x86_64, unpack the complete program directory and run `UrltraKB.exe`.
On Debian 13.6 x86_64 GNOME/X11, extract the program archive and run `UrltraKB`.
Runtime archives and their top-level directory are named **UrltraKB**. Full
source/build materials are a separate `UrltraKB-VERSION-materials.zip` download;
licenses and the companion archive checksum remain available in About.
Keep the program's resources together. No developer Python, Node or Rust runtime
is needed to run a complete portable build. Other Linux desktops, Wayland,
macOS and ARM have not been accepted as distribution targets.

Use the knowledge-base manager to create a new KB or open an existing one.
Registered locations and locations beneath the configured KB root appear with
their full paths. Two KBs may share a directory name; select by path. Switching
the active KB changes what you browse and where new work is submitted. It does
not stop work already submitted for another KB.

Knowledge-base configuration remains in `.openkb/config.yaml`, KB credentials in
its `.env`, and completed conversations in `.openkb/chats`. Global configuration
uses the existing OpenKB user configuration directory. Task summaries and
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
choices use the existing Qt `OpenKB/OpenKB` application identity in the user's
platform settings, separately from KB/model/credential configuration. The reader's
zoom is available on the Knowledge page and applies to content previews as well.

See [implementation and evidence](desktop-workbench.md) for the scope, assets,
actual application screenshots and reproducible source acceptance commands.

## Import and maintain knowledge

Import files, folders, or URLs from **资料** (Documents). Supported document inputs
include PDF, Markdown, Word, PowerPoint, Excel, HTML, text and CSV. Each task
shows its KB, current stage and individual outcomes. An already indexed input
may be skipped. A batch can retain completed documents while reporting failed
or unprocessed ones.

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

Below the composer, **本对话 tokens** shows reported cumulative input cache hits,
input cache misses, output tokens and reasoning tokens, including model requests
made while using tools. The model is asked to return streaming usage; counters
update as each request reports its usage, not as each character appears. Output
already includes reasoning tokens. **—** means no usable count was reported;
older conversations are not retrospectively estimated. Counts are saved with the
conversation, including reported requests from an unfinished turn. SDK/provider
normalization may report zero for unsupported detail fields.

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

## Version, source and licenses

Open **Application menu (⋯) → 关于 UrltraKB · 源码与许可** to inspect and copy the
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
