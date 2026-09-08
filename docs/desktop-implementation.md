# Native desktop implementation evidence

Target: GitHub issue #1 and the confirmed P0–P5 handoff in issue #10.
Baseline: `0cec254bb2f37adff8b1af2e1bd2802ea56910ee` (main).
The existing local CLAUDE.md, CONTEXT.md and CodeGraph changes are unrelated.

## P0 — compatibility baseline (2026-09-07)

Host: Debian 13.6 x86_64. Python: CPython 3.12.13.
Installed the repository's frozen dependencies explicitly with
`uv sync --frozen --extra dev --extra api`; no dependency pins changed.
The initial environment lacked pytest, Ruff and mypy; no checks were claimed
until installation completed.

| Check | Baseline result |
| --- | --- |
| `pytest -q` | 1249 passed, 2 warnings, 14.17 seconds |
| `ruff check .` | Passed |
| `ruff format --check .` | Passed |
| `mypy openkb` | Passed, 56 source files |

The declared CLI/API minimum remains Python 3.10. The lock contains separate
Python/platform resolutions; the baseline execution above only tests 3.12.

Accepted renderer input: commit `e581f76519dbda8b6cfdc485b8b06bb0c7742696`.
Accepted portable input: commit `b039999d346ca9a5deb72670d6696ee6dfdbccb1`.
Both source worktrees and their locked resources are locally available.
These prototypes establish the selected engineering direction, not product
acceptance. Windows 11 product validation requires its own environment.

## Gates

P1 establishes shared operations and complete write protection; P2 integrates
isolated workers and native rendering; P3 completes the daily workflow; P4
retires the browser product only after that workflow passes; P5 validates the
actual Debian and Windows packages and matching source/licence materials.
Unexecuted checks and missing platform evidence remain pending.

## P1 checkpoint — shared operations and coordination (2026-09-07)

This is an implementation checkpoint, not completion of issue #1 or the P1 gate.
Creation/opening, document import, page read/save, settings and answer/chat
operations now have shared application entry points. Legacy CLI/REST adapters
retain their response and save-name policies. Settings patches preserve omitted,
set and clear states and roll back both config and credentials on failure.
Optional execution contexts capture complete settings and environment in memory;
core readers use copies of the captured values throughout that execution.

Document conversion reads a verified private copy while preserving the original
registry identity and relative-image directory. Source and copied-image versions
are checked during preparation. API upload ownership and the full watcher input
protocol still need migration before those paths are connected to the GUI.

The first parallel review found an API event-loop deadlock, a URL fetch/cleanup
race, and stream teardown that could release write protection before the SDK
settled. Fixes offload synchronous read locks, restore the whole URL item lease,
and explicitly close nested stream generators before releasing chat leases.
It also found duplicate display-type mapping and missing config parsing on open;
these have been corrected. The sync/async lock-flow duplication remains a review
judgment call, and the full feature remains incomplete.

| Check | Actual result |
| --- | --- |
| Full `pytest -q` | 1261 passed, 2 existing mocked-coroutine warnings, 12.28 seconds |
| `ruff check .` / `ruff format --check .` | Passed / 135 files formatted |
| `mypy openkb` | Passed, 67 source files |
| Real Debian spawn/OS lock probe, Python 3.12.13 | 16.03 s same-KB contention; cancellable waiter, independent KB, process reaping passed |
| Real Windows 11 x86_64 spawn/OS lock probe, Python 3.12.13 | 16.02 s same-KB contention; cancellable waiter, independent KB, process reaping passed |

The Windows host reports Windows 11 build 26100.9168. Its probe uses an isolated
Python installation and venv in `OpenKB-native-20260907`; existing installations
were not replaced. The portable desktop package, graphical interaction and real
model/generator distribution acceptance have **not** been run there yet.

Remaining P1/P2 work includes the controlled repair path, structured per-item
outcomes, actual worker isolation/configuration tests and reliable task results.
Other management/generator writers must receive full-operation protection before
being exposed to concurrent desktop tasks. P3–P5 and browser-product retirement
remain pending; prototype evidence is not substituted for product acceptance.

## P2 checkpoint — native execution path (2026-09-07)

The desktop entry point now opens a real Qt Widgets workbench. It calls local
application operations without a REST service. Initial controls cover KB
opening/creation, file/directory import, page reading/body saving, one-off
questions, conversation continuation and task/retained-resource observation.
This remains a checkpoint: it is not the complete daily workflow or the P2 gate.

The task manager owns logical batches. Each unit runs in a new spawn child;
waiting for a busy KB releases the worker slot. The first actual execution
acknowledges its complete configuration snapshot before business work starts;
subsequent units retain it in memory. Independent control/progress channels,
correlated per-unit receipts and content-free history distinguish business
results, unavailable answer text and process cleanup. Repair-blocked execution
halts the remainder; missing receipts do not trigger replay.

The first runtime review found incorrect page-resource paths, continued batches
after failed recovery, stale historical process waits and unavailable answer
text after lost final delivery. These paths have been corrected. Configuration
validation preserves legacy empty-string and zero/negative-threshold values
while rejecting incorrectly shaped data. Controlled repair reuses that boundary.

| Check | Actual result |
| --- | --- |
| `pytest -q` at this checkpoint | 1271 passed, 2 existing warnings, 35.87 seconds |
| Ruff check / format check | Passed / 157 files formatted |
| `mypy openkb` | Passed, 84 source files |
| Real SDK against a controlled local HTTP model | Dual-KB model/key/header isolation; fixed batch snapshot; latest queued task; parent environment/cwd unchanged |
| Real child termination during a model request | Interrupted outcome; remaining unit not replayed; child reaped |
| Busy KB with one worker slot | Independent KB completed; waiting task stopped without writes |
| GNOME/X11 native window | Chinese, inline fraction, block formula, flowchart, table and code displayed; body saved via child; explicit exit reaped task processes |

The window smoke check found a quit race (save-completion callbacks scheduling
reads after shutdown) and a close-event veto of explicit application quit;
both were fixed and the smoke check then completed successfully. A screenshot
and logs are local evidence under `/tmp/openkb-native-first.png` and
`/tmp/openkb-desktop-smoke-2.log`; they are not portable-package acceptance.

The renderer uses the accepted MathJax/Merman/resvg code and font adaptations.
`prepare_desktop_assets.py` verifies Node/font hashes, installs the exact npm
lock without install scripts and builds the locked Rust helper. PySide6
Essentials/shiboken 6.11.2 are isolated in the `desktop` extra. Full rendering
coverage, first product freezing, Windows GUI/package evidence, remaining
management/generator/watch/settings workflows, browser retirement and matching
source/licence distributions remain pending.

## P2 checkpoint — first product builds (2026-09-08)

The actual workbench now freezes into native Windows and Debian program
directories with separate desktop, CLI, REST and explicit acceptance entry
points. These are internal builds, not release archives. Instructions and
limitations are in `packaging/desktop/README.md`.

The second parallel review found stale answer attribution, stale asynchronous
rendering, Markdown rewriting inside code/link targets, unsafe save-version
refresh, stale conversation reads, encoded wikilink paths and stale conflict
drafts. Fixes use token boundaries, explicit view generations and the exact
committed Page carried by the save receipt. Page contents are omitted from
durable task summaries. Saving also updates the reader from that receipt.
Conflict handling offers current-content comparison, explicit reconfirmation
and draft export. Waiting local reads release their thread slot; they never
retry an operation that already began. Concurrent renderers publish complete
images atomically instead of exposing in-progress helper output.

| Check | Actual result |
| --- | --- |
| Full pytest checkpoint | 1273 passed, 2 existing warnings, 25.27 seconds |
| Ruff check / format check | Passed / 166 files formatted |
| mypy | Passed, 88 source files |
| Source workbench | Continued editing during save; external write before save observation; conflict draft retention; saved reader content; independent-KB navigation under read contention; clean exit |
| Debian frozen application | Seven document formats converted, compiled, registered and read; query saved; two chat turns persisted; actual SDK with controlled local HTTP fixture |
| Windows frozen application in interactive session 1 | The same seven-format import/query/chat flow passed; explicit exit completed |
| Product renderer corpus, source and both frozen platforms | 32 samples × 2 themes × 4 scales; 240 rendered outputs and 16 expected errors per run |
| Concurrent renderer output | 16 simultaneous same-formula renders produced complete readable images |

The document fixtures cover Markdown, HTML, PDF, DOCX, PPTX, XLSX and XLS.
They are reused inputs from the earlier dependency probe; all reported product
operations were newly executed through the actual task workers. The renderer
corpus is committed under `tests/fixtures/native-render-corpus.json` and was
newly executed through the product Markdown parser and QTextDocument display.
Debian visual inspection covered all formula examples and the nine diagram
families in light/dark presentation; larger-scale output and Windows images
still need their complete visual verdict recorded. Parse/output checks alone
are not semantic approval.

Local logs include `/tmp/openkb-frozen-import-1.log`,
`/tmp/openkb-frozen-corpus-1.log` and `/tmp/openkb-p2-full.log`.
Windows evidence is isolated under `OpenKB-native-20260907/interactive-model-1`
and `interactive-corpus-1`; both commands exited 0 on 2026-09-08 in the logged-in
desktop session. The temporary acceptance scheduled task was removed after
completion. The last local read-contention/image-publication fixes postdate
these first frozen runs and require rebuilding at the next package checkpoint.

P2 is not declared complete: final package revisions, long-document indexing,
render-helper parent-loss behavior and the remaining platform acceptance
details need completion. Full settings, maintenance, generators and watching
in the workbench, browser-product retirement, clean-system replacement tests
and matching source/licence distributions remain P3–P5 work.

## P3/P4 implementation and P5 package checkpoint (2026-09-08)

The subsequent shared/native slices implement document management, settings,
conversation management, maintenance/recovery, generators and exports, stable
file/URL inputs, independent directory watching, manual retry and history
cleanup. Directory-generation leases protect removed/recreated KBs, including
CLI/API operations. The React product, static hosting and obsolete web entry
point have been removed; independent REST and HTML artifact contracts remain.
These implementation results do not establish final distribution acceptance.

Source exports identify the exact Git commit and installed version. The native
About dialog and REST material endpoints validate matching, immutable copies
of distribution materials; actual release materials are still being assembled.
The frozen inventory now accounts for executable Python archives, collected
files and primary component provenance on both systems. Nested native component
source/licence reconciliation remains separate work.

| Latest targeted evidence | Actual result |
| --- | --- |
| `8eddcd7` frozen Debian GNOME/X11 | Native workflow and 256 rendering outputs/error cases passed technical checks |
| `8eddcd7` frozen Windows 11, interactive session 1 | Native and corpus checks passed; model workflow exposed an artifact export failure |
| Artifact export regression | Four tests passed on Debian and Windows; a profile containing only global lifecycle state no longer counts as a KB, while incomplete initialization remains protected |
| Real Qt deletion race | Before the fix, 26 of 30 runs displayed a stale-read error; 30 subsequent runs passed without unexpected dialogs |
| Delayed open result after target deletion | Actual Workbench/LocalIO probe now retains the previously active KB |
| Source GNOME/X11 controlled model after export/read fixes | 22 checks, 34 tasks, every task process reaped, no unexpected dialogs |
| Current Ruff / mypy / module size | Passed; mypy covers 140 source files |

The controlled model run exercises actual HTTP SDK requests, seven document
formats, 20-page PageIndex output, URL preparation, query/chat, generation,
watching and saved outputs. Its model responses are fixtures; it does not prove
a connection to a real provider. The full pytest suite has not been repeated
since the earlier checkpoint and remains a final completion check.

Windows analysis also exposed collection of SDK utility DLLs from the build
host's PATH. Restricting the freezer's PATH to Python and Windows system paths
removed that dependency, and the resulting program passed native/corpus checks.
The final committed fixes still require fresh frozen builds and acceptance on
both targets. Complete visual verdicts, actual tray/exit/replacement scenarios,
clean-system and live-provider evidence, matching source/licence materials and
final archives remain pending.

## Frozen renderer and shutdown checkpoint (2026-09-08)

Both actual program directories exported from `534e35f` passed the native
workflow and the 22-check controlled HTTP model workflow, including the corrected
Windows Unicode fixtures. Windows ran in the logged-in interactive session 1.
Debian ran as a non-root user in a Debian 13.6 runtime rootfs containing no Python,
Node, Rust or browser commands, with the host GNOME/X11 display socket attached.
That establishes isolated runtime dependencies, not a fresh whole-machine image.

The corpus now contains 37 cases, each rendered at four scales and two themes
on each platform. All 592 outputs were visually approved by inspecting changed
variants and matching unchanged RGBA pixels to previously inspected originals.
Per-case expectations, verification methods and original PNG hashes are in
`docs/desktop-evidence/render-534e35f.json`. The 16 expected unsupported-input
results on each platform remain errors with source preserved; they are not
counted as successful rendering of supported input.

This inspection exposed and corrected C4 stereotype-label squeezing, inline
formula baselines, and Windows Gantt ticks shifted by the host's eight-hour UTC
offset. The fixed Windows ticks match the task dates. Actual Qt paint and text
line metrics measure 48 inline variants per platform; maximum baseline error is
0.47 px on Debian and 0.78 px on Windows. Raised glyphs intentionally remain above
the baseline; tall formulas and the following Chinese paragraph do not collide.

Shutdown review then found that waiting to exit from the tray left the main
window hidden and disabled task diagnostics. The fix reveals the task panel,
keeps task selection, result copying and cooperative stop available, and disables
new business controls. Existing modal dialogs are revoked, including cancellation
of a pending KB deletion. Closing the waiting window preserves observation.
All child Markdown readers, including artifact dialogs, enter the render shutdown
barrier; task completion no longer starts new reading or rendering during exit.

The real Qt/spawn probes failed before these fixes and passed afterwards. Three
new subprocess scenarios cover waiting, stopping before execution, and pending
deletion; each then starts a fresh process over the same KB/history without
replay or restored watching. The latest targeted checks passed on Debian (9)
and Windows (8); GNOME/X11 also passed the visible no-tray fallback and waiting
case. These shutdown fixes postdate the `534e35f` frozen packages and need a new
frozen run. Real OS tray clicks, in-flight model cancellation, external process
observation, program replacement, live-provider/clean-Windows acceptance and
complete distribution materials are still outstanding.

## Production lifecycle and source audit checkpoint (2026-09-08)

The program directories exported from `6b7139e` now pass all six frozen
lifecycle/restart scenarios and the native and controlled HTTP model workflows
on Debian and Windows. The 22-check model workflow exercises real worker
processes and SDK HTTP calls; responses remain local fixtures. Windows ran in
interactive session 1. Debian used a non-root Debian 13.6 runtime rootfs with
GNOME/X11 supplied by the host. Neither result establishes a clean Windows
installation or a fresh whole-machine Debian image.

The following additional checks launch the production application event loop
and use real OS mouse/keyboard input, outside the built-in Qt verifier:

| Scenario | Evidence |
| --- | --- |
| Debian no-tray close and idle Quit | Window stayed visible with the fallback explanation; explicit Quit ended the container |
| Windows native directory cancellation, tray reopen and Quit | Three fresh processes passed cancellation, mouse tray reopening and their combined sequence |
| Safe stop during a model request, both systems | First active document completed after stop; pending second document never started; observed application/worker PIDs ended |
| Fresh launch after safe stop, both systems | Stopped history and completed results remained; KB hashes and model request counts did not change |
| Windows watching while hidden | Adding a raw file while the workbench was hidden triggered a model task; a physical tray click reopened the running task |
| Windows wait-for-completion Quit | Active watched task completed; a file added after Quit was not accepted; all observed program processes ended |
| Windows launch after watching exit | Watch list remained empty and the unprocessed raw file did not trigger work |
| Same-version program replacement, both systems | Complete copies of 10,832 Debian / 11,331 Windows program files were verified and renamed into the same launch path after exit; 240 / 246 retained data and evidence files stayed unchanged through relaunch |

The production safe-stop cases each use two documents, with no earlier completed
item at the time stop is requested. Broader completed/active/pending batch
semantics remain covered by the shared application tests. The replacement
checks replace the same build version; they do not claim a cross-version data
migration test. Failed early Windows input automation selected the wrong recent
KB or missed native controls; successful reruns use the owned process/window
and actual OS input. Those automation failures are not counted as product bugs
or passing acceptance.

Windows produced another 296 corpus outputs: 284 were RGBA-identical to the
previously reviewed `534e35f` images; all 12 changed state-diagram variants were
visually approved. Debian retains the prior renderer verdicts, with current
native/model rendering checks passing. Current summaries and original evidence
hashes are in `docs/desktop-evidence/acceptance-6b7139e.json`; raw local evidence
is retained under `packaging/desktop/build/evidence/6b7139e`.

The final full pytest run at `eb40577` passed 1,470 tests in 49.02 seconds.
One earlier failure was an initialization test setup acquiring the KB lock
before the creation lifecycle; two fixtures now follow the supported lock
order. Product code did not change in that correction. Existing unawaited
coroutine warnings remain. Ruff check/format (248 files), mypy (141 source
files) and the module-size check passed.

Source archive hashes have been rechecked and recorded in
`docs/desktop-evidence/source-audit-6b7139e.json`. Additional extraction inspected
946 source archives and retained 1,781 licence/copyright/NOTICE files. The Eigen
ZIP checksum changed, but all 1,784 files match the exact required upstream Git
commit; its full Git tree is now archived, with the original checksum mismatch
and explicit rebuild-input requirement retained. These are source-audit inputs,
not a declaration that every optional dependency was linked or that all
corresponding-source and licence conditions have been met.

At this checkpoint P5 remained incomplete. The subsequent live-provider and
Sandbox decisions are recorded below. Exact resource terms, applicable embedded
native-library source and patch mapping, installed notice reconciliation, and
final matching source/licence/NOTICE/build assets still need completion before
formal portable archives can pass distribution review. No release has been
published.

The two production Windows QA knowledge bases were subsequently unregistered,
and their two task summaries/receipts archived before clearing them through
the task-history interface. The original global YAML was restored only after
its exact pre-test SHA256 matched; other profile fields were not guessed or
rolled back. All scheduled tasks created for these OS checks were removed.
The retained screenshots/records describe the earlier acceptance runs, before
this deliberate QA cleanup.

## Live DeepSeek acceptance and artifact reading (2026-09-08)

The user supplied `https://api.deepseek.com` and `deepseek-v4-flash`, and explicitly
declined Windows Sandbox. No Windows feature was enabled. Tests use the existing
Windows 11 x86_64 host and the non-root Debian 13.6 runtime rootfs described above;
these results do not claim a clean whole-machine installation. The wire model is
`deepseek-v4-flash`; the existing LiteLLM configuration uses
`openai/deepseek-v4-flash` for the custom OpenAI-compatible endpoint. The provider
model-list request returned HTTP 200 and included the requested model.

Both `6b7139e` production applications ran through their ordinary event loop with
OS mouse/keyboard input and the actual external provider. Each completed eight
KB tasks: a Chinese synthetic document import, saved grounded question, two
turns of one conversation, Skill generation, two HTML deck requests, and import
of the public Python.org executive-summary HTTPS article. The question and
conversation retained the document's device identifier, 17-day interval and
responsible person's name; saved sessions contain exactly two completed turns.
Skill and eight-slide deck files were retained, and the final deck passed the
existing validator without errors or warnings.

The first deck request asked for four slides, below the existing five-slide
minimum. Both programs correctly retained its completed output and exposed the
quality error/warning. A separately named eight-slide request passed. One early
Windows input driver also selected the artifact-file combo as a candidate for
the generation-type combo; the driver was corrected before the deck request.
These are recorded as QA setup issues, not product failures or clean passes.

Explicit Quit ended the observed program processes. After fresh launch and
opening the same KB, all 25 Debian / 27 Windows retained data files had identical
hashes, task IDs stayed unchanged, and both completed conversations were intact.
The applications then quit normally again. Windows QA registration and eight
archived task records were removed; the original global YAML was restored only
after comparing all other fields with the private pre-test backup. Its original
SHA256 matched. Seven QA scheduled tasks were removed. API keys, private profile
backups and raw private logs are excluded from the retained evidence.

Reading the generated Skill exposed a display defect: the YAML header was being
rendered as a large Markdown heading. The reading view now strips valid nonempty
mapping frontmatter only when both delimiters are independent `---` lines; the
source tab and file bytes remain unchanged. A native UI regression failed before
that fix and passed afterwards. Two malformed-delimiter cases were then added
following both review axes, failed before the guard and passed after it. The
source native workflow passed all 14 checks. Full pytest, lint/format and mypy
results, plus new frozen-program evidence, are recorded when available below.

The detailed live-provider record is
`docs/desktop-evidence/live-deepseek-6b7139e.json`, with 105 whitelisted evidence
files under `packaging/desktop/build/evidence/live-deepseek-6b7139e`. Its production
commit is intentionally still `6b7139e`; it is not relabelled as a test of the
subsequent reading fix.

Seven renderer crates lacking standalone licence filenames now have explicit
full-text mappings: six Merman crates inherit the licence of their exact recorded
workspace commit, and selectors 0.37.0 names MPL-2.0 in its source header and
manifest, supplemented with Mozilla's canonical full text. Original notices are
retained. Both review axes confirmed the narrow reading correction; the standards
review also verified these licence mappings.

Formal distribution remains open. The remaining audit must identify the exact
sources for the Linux standalone GCC runtime libraries, map Qt's actual source
and patches, and resolve the exact NewCM resources' terms and modifiable source
form. Windows static GCC code, Pillow, and python-build-standalone require their
own component-specific assessment; unmodified general-purpose tools are not
automatically required in the source asset. The five distribution-material kinds
and final portable archives have not yet been assembled or published.

After the delimiter guard, full pytest passed 1,470 tests in 49.56 seconds with
two existing unawaited-coroutine warnings. Ruff check/format passed for 248 files,
mypy passed for 141 source files, and the module-size gate passed in pytest.
