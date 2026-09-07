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
