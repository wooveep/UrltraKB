# UrltraKB workbench: design and source acceptance

This implements [issue #12](https://github.com/wooveep/UrltraKB/issues/12).
The accepted Qt Widgets, shared application interfaces, isolated task workers and
browser-free rendering architecture remain in place. The desktop uses the display
name **UrltraKB**, while `openkb`, Python package/distribution names, `.openkb`,
Qt `OpenKB/OpenKB` settings identity, CLI and REST contracts remain compatible.
Previously released program names and original third-party notices are unchanged.

## Composition

- `brand.py` and bundled original SVGs separate display identity from compatibility
  identifiers. Asset provenance is in `openkb/desktop/assets/brand/README.md`.
- `appearance.py` applies a shared palette and native control styling. Follow
  System is the initial choice; explicit Light/Dark and sidebar preference are
  stored as application-local Qt settings, separately from credentials and KBs.
- `shell.py` owns the compact top bar, 216/60 logical-pixel navigation and page
  selection. Automatic compact mode below 1080 logical pixels never persists over
  the wide-window preference; its toggle remains reachable.
- `workspaces.py` composes Overview, Documents, Knowledge, Conversations, Artifacts,
  Tasks and Settings using existing controls. Common management forms support
  page embedding; Escape cannot dismiss an embedded page. Destructive confirmation,
  conflict review, recovery and detailed task inspection retain their dialogs.
- Knowledge directory and page sources are local, optional panes. The reading
  and editing controls persist while navigating, so toggles preserve drafts.
  Task-owned outputs are displayed exclusively with their task's full KB location.
- Tasks and watches remain application-wide. Task submission does not change the
  current page. Global status leads to details and manual retry; shutdown selects
  Tasks and leaves observation and cooperative stop usable while disabling new work.

## Reproduce source acceptance

Use the existing pinned development and desktop dependencies plus the prepared
local rendering assets, as described in the desktop build guide. All commands
below create dedicated fixture KBs and profiles; output directories must be new.
No paid model endpoint or real credentials are used.

```bash
.venv/bin/pytest -q tests/test_desktop_workbench.py tests/test_desktop_lifecycle.py
QT_QPA_PLATFORM=offscreen .venv/bin/python -m openkb.desktop.verification \
  --workbench --output /tmp/urltrakb-workbench
QT_QPA_PLATFORM=offscreen .venv/bin/python -m openkb.desktop.verification \
  --workbench-restart /tmp/urltrakb-workbench --output /tmp/urltrakb-restart
QT_QPA_PLATFORM=offscreen QT_SCALE_FACTOR=1.5 .venv/bin/python \
  -m openkb.desktop.verification --workbench --output /tmp/urltrakb-hidpi
QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/verify_desktop_model.py \
  --urls --output /tmp/urltrakb-business
QT_QPA_PLATFORM=offscreen .venv/bin/python -m openkb.desktop.verification \
  --corpus tests/fixtures/native-render-corpus.json --output /tmp/urltrakb-corpus
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy openkb
.venv/bin/pytest
```

The workbench runner drives the real Qt window, checks visible pages and keyboard
controls, verifies a saved draft and failure feedback through real tasks, switches
between same-named KBs, simulates palette input at the Qt boundary, and checks
preferences in a fresh process. Its screenshot matrix contains all seven pages
in both themes at 1366×768, 1920×1080 and 900×650 logical pixels. The same matrix
runs at 150% scaling. The existing business runner now exercises document,
conversation, artifact and ordinary settings management through the embedded
pages; restricted recovery and destructive confirmations remain modal.

## Actual application screenshots

Captured on 2026-09-08 with PySide6/Qt 6.11.2, Linux x86_64, the offscreen Qt
platform and 150% scale. These are actual Workbench captures from isolated sample
KBs, including empty source/artifact lists and a deliberately failed revision
check. Blank lists are empty states, not simulated data. Files are retained
without image editing; screenshot pixels include Qt's device scaling.

| Page | Screenshot |
| --- | --- |
| Overview, light, 1366×768 logical | [Overview](desktop-evidence/workbench/overview.png) |
| Documents, dark, 900×650 logical | [Documents](desktop-evidence/workbench/documents.png) |
| Knowledge, dark, 900×650 logical | [Knowledge](desktop-evidence/workbench/knowledge.png) |
| Conversations, dark, 1366×768 logical | [Conversations](desktop-evidence/workbench/conversations.png) |
| Artifacts, light, 900×650 logical | [Artifacts](desktop-evidence/workbench/artifacts.png) |
| Tasks, light, 900×650 logical | [Tasks](desktop-evidence/workbench/tasks.png) |
| Settings, dark, 900×650 logical | [Settings](desktop-evidence/workbench/settings.png) |

This is source-run Qt behavior and visual evidence. Offscreen tray-menu activation
checks application behavior, not physical OS tray integration. It does not claim
fresh-machine Windows/Debian portable-distribution acceptance or a new release.
The accepted rendering engine and grammar are unchanged; the native corpus
scenario continues to cover Chinese, inline/display formula structure, Mermaid,
tables and code through the actual reader.


## Recorded checks and review

Source acceptance on 2026-09-08 (Python 3.12.13, Qt 6.11.2):

| Check | Result |
| --- | --- |
| Full pytest suite after review fixes | 1,471 passed, 57.49 seconds |
| Workbench, shutdown/restart, module size | 9 passed |
| Source export, Markdown and render-process regressions | 18 passed |
| Controlled local HTTP/model acceptance | 20 checks passed, including management through pages |
| Actual Qt corpus | 296 cases across both themes at 100/150/200/400% |
| Expected unsupported-input fallbacks | 16 cases, verified as explicit source fallbacks |
| Inline formula baseline checks | Maximum measured error 0.470 logical pixels |
| Screenshot matrix | 42 views at 100%; repeated at 150% device scale |
| Ruff / formatting / mypy | Passed |

Pytest reported a compiler-coroutine warning in `test_remove.py` and a coroutine
warning during final garbage collection; all tests completed successfully.
The corpus runner leaves its exhaustive visual verdict separate from structural
checks. This change includes representative screenshot inspection, not a renewed
qualification of every supported diagram at every scale.

The independent Standards review identified one low-severity page-order coupling
in shutdown; it was removed. The Spec review identified stale inherited settings
and a hidden answer when submitting from history; both were reproduced and fixed,
with behavior regressions in the actual Workbench runner. The follow-up reviews
reported **Standards: 0 remaining; Spec: 0 remaining**.
