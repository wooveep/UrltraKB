# Native desktop guide

The desktop opens local knowledge bases in their existing format. It uses Qt
Widgets for its interface and local static rendering for mathematics and Mermaid.
The CLI and independent REST API continue to work with the same data.

## Launch and data locations

On Windows 11 x86_64, unpack the complete program directory and run `OpenKB.exe`.
On Debian 13.6 x86_64 GNOME/X11, extract the program archive and run `OpenKB`.
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

## Import and maintain knowledge

Import files, folders, or URLs from the workbench. Supported document inputs
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

Browse summaries, concepts, entities and explorations. The page context shows
source material, outgoing links and backlinks. Missing or ambiguous targets are
reported rather than linked to an arbitrary page. Code blocks, tables, images,
Chinese text, mathematics and the supported Mermaid families display natively.

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

Query produces a grounded answer using the current KB. Chat streams a turn and
can resume existing CLI conversations. Completed turns retain the established
session format and the session's model/language choices. Concurrent attempts to
write the same conversation wait for exclusive access.

Save an answer or export a transcript to keep it as an exploration. Desktop
exports choose a new name so that repeated saves preserve prior outputs.
Incomplete streamed text remains readable and copyable for the current run;
an interrupted answer is not recorded as a completed conversation turn.

## Generate and export outputs

Generate Skills, HTML slide decks and the existing HTML knowledge graph from
the KB. The artifact list provides access to saved files and export actions.
An existing Skill or deck name requires a new name or explicit consent to archive
and replace its output. Graph generation retains its established fixed path.

Opening an HTML deck or graph in the default external browser is an explicit
preview action. The workbench itself does not embed a browser. Advanced Skill
evaluation, validation, history and rollback remain available in the CLI.

## Settings and task outcomes

Global and KB settings expose the existing model, language, concurrency and
provider configuration. Secret fields distinguish keeping, replacing and
clearing a value. Desktop credentials prefer the KB, then the environment from
which the application launched, then global settings. The CLI retains its
environment-first behavior. Desktop setup uses API keys; subscription login
continues to be a CLI capability.

A queued task uses settings resolved when business execution first begins,
after obtaining access to the KB. A running task keeps that configuration;
an entire batch shares its initial configuration across its documents. A new
watch-triggered task or manual retry captures its own settings.

The task list distinguishes waiting, processing, stopping and terminal results.
Inspect completed, skipped, failed and unprocessed items as well as retained
outputs and quality diagnostics. Stopping prevents later units from starting
and lets required recovery/commit work finish safely. It does not undo earlier
completed work. A worker exit without a reliable result is shown as interrupted
or unconfirmed, not inferred to be a success.

Manual retry previews what remains and creates a new task. Failed work is not
automatically replayed. Clearing task history removes summaries, not generated
artifacts or conversation records.

## Watching and exit

Enable directory watching explicitly for a KB's `raw/` directory. The native
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

Open **Help → About OpenKB · Source and licenses** to inspect and copy the
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
