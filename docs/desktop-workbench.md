# UrltraKB workbench: design and source acceptance

This implements [issue #12](https://github.com/wooveep/UrltraKB/issues/12).
The accepted Qt Widgets, shared application interfaces, isolated task workers and
browser-free rendering architecture remain in place. The desktop uses the display
name **UrltraKB**, while `openkb`, Python package/distribution names, `.openkb`,
Qt `OpenKB/OpenKB` settings identity, CLI and REST contracts remain compatible.
Portable entry points and archive roots use `UrltraKB`; original third-party notices are retained.

## Composition

- `brand.py` and bundled original SVGs separate display identity from compatibility
  identifiers. Asset provenance is in `openkb/desktop/assets/brand/README.md`.
- `appearance.py` applies a shared palette and native control styling. Follow
  System is the initial choice; explicit Light/Dark and sidebar preference are
  stored as application-local Qt settings, separately from credentials and KBs.
- `shell.py` owns the compact top bar, 224/64 logical-pixel navigation and page
  selection. Automatic compact mode below 1080 logical pixels never persists over
  the wide-window preference; its toggle remains reachable.
- `workspaces.py` composes Overview, Documents, Knowledge, Conversations, Artifacts,
  Tasks and Settings using existing controls. Common management forms support
  page embedding; Escape cannot dismiss an embedded page. Destructive confirmation,
  conflict review, recovery and detailed task inspection retain their dialogs.
- `fonts.py` loads the supplied `assets/fonts/` files into the application only.
  Source Han Sans CN 2.005 uses the supplied static Regular (400), Medium (500)
  and Bold (700) faces. Source Code Pro 2.042 supplies the same code weights;
  its 1.062 Italic/Bold Italic faces preserve emphasized code. The base QFont
  leaves its style name and variable axes unset so QSS and Markdown inherit
  real weights, with Source Han Sans as the explicit Chinese code fallback.
  Only manifest-listed faces and notices are included in wheels and runtime
  archives; unused reference fonts stay in the source workspace.
  Windows desktop entry points select Qt's bundled FreeType font engine for
  consistent variable-font rasterization. Explicit Qt platform arguments or the
  `QT_QPA_PLATFORM` environment setting take precedence; system settings are untouched.
- The flat, neutral shell uses a full-height navigation rail and a quiet context
  bar. Conversations use a centered readable column, distinct user messages and a
  bottom composer. Enter sends, Shift+Enter inserts a line, and IME confirmation
  does not send a message.
- `drawer.py` keeps conversation history and page sources in local overlays with
  an interruptible 180 ms fade/slide. Escape and outside clicks close a drawer and
  return focus; changing workspaces or KBs retires it without losing drafts.
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

The workbench runner also checks the supplied fonts, intermediate fade frames,
reversed transitions, drawer resizing/focus, draft retention and IME-safe submission.
It drives the real Qt window, checks visible pages and keyboard
controls, verifies a saved draft and failure feedback through real tasks, switches
between same-named KBs, simulates palette input at the Qt boundary, and checks
preferences in a fresh process. Its screenshot matrix contains all seven pages
in both themes at 1366×768, 1920×1080 and 900×650 logical pixels. The same matrix
runs at 150% scaling. The existing business runner now exercises document,
conversation, artifact and ordinary settings management through the embedded
pages; restricted recovery and destructive confirmations remain modal.

## Actual application screenshots

Captured on 2026-09-08 with PySide6/Qt 6.11.2, Linux x86_64, the offscreen Qt
platform and 100% scale. These are actual Workbench captures from isolated sample
KBs, including empty source/artifact lists and a deliberately failed revision
check. The conversation screenshot uses a presentation fixture, not an actual model response.
Blank lists are empty states. Files are retained
without image editing; screenshot pixels include Qt's device scaling.

| Page | Screenshot |
| --- | --- |
| Overview, light, 1366×768 logical | [Overview](desktop-evidence/workbench/overview.png) |
| Documents, light, 1366×768 logical | [Documents](desktop-evidence/workbench/documents.png) |
| Knowledge, light, 1366×768 logical | [Knowledge](desktop-evidence/workbench/knowledge.png) |
| Conversations, light, 1366×768 logical | [Conversations](desktop-evidence/workbench/conversations.png) |
| Artifacts, light, 1366×768 logical | [Artifacts](desktop-evidence/workbench/artifacts.png) |
| Tasks, light, 1366×768 logical | [Tasks](desktop-evidence/workbench/tasks.png) |
| Settings, light, 1366×768 logical | [Settings](desktop-evidence/workbench/settings.png) |

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
| Full pytest suite after review fixes | 1,474 passed, 60.06 seconds |
| Controlled local HTTP/model acceptance | 22 checks passed, including management through pages |
| Actual Qt corpus | 296 cases across both themes at 100/150/200/400% |
| Expected unsupported-input fallbacks | 16 cases, verified as explicit source fallbacks |
| Inline formula baseline checks | Maximum measured error 0.605 logical pixels |
| Screenshot matrix | 42 views at 100%; repeated at 150% device scale |
| Ruff / formatting / mypy | Passed |

Pytest reported a compiler-coroutine warning in `test_remove.py` and a coroutine
warning during final garbage collection; all tests completed successfully.
The corpus runner leaves its exhaustive visual verdict separate from structural
checks. This change includes representative screenshot inspection, not a renewed
qualification of every supported diagram at every scale.

The independent Standards review reported no findings. The Spec review caught a
Chinese fallback to a system font inside code; both the source editor and Markdown
code now name Source Han Sans after Source Code Pro. Actual glyph runs verify both
families and the weight axis. Follow-up review reported **Standards: 0 remaining;
Spec: 0 remaining**.


The font-weight follow-up reproduces the earlier 400/500 glyph-outline collapse
on Windows and Linux. Its native verifier additionally checks the real face name
and OS/2 weight after QSS inheritance and actual Markdown bold/italic rendering.
The original screenshot/check table above records the preceding redesign; the
matching build's delivery report contains follow-up evidence and package hashes.

## Continuous chat and compact layouts

The desktop composer uses automatic multi-turn conversations with no mode or save
checkbox. The history drawer opens conversations with a single click, while export
and deletion live in its More menu. In-progress work shows a status, not the raw
model trace. Completed answers use the SDK terminal output; explicit reasoning
envelopes are filtered without removing literal Markdown code examples.

Ordinary buttons have visible borders, sidebar labels use 18 px text, and action
rows wrap as space narrows. The layout capture matrix now includes 720 × 600
logical pixels. Native chat acceptance covers saved multi-turn history, tagged
reasoning exclusion, duplicate Enter prevention and a background result arriving
after the user starts a different conversation.

## Populated document tables

Native headers explicitly reset the padding inherited from `QAbstractItemView`.
That inherited padding shifted header sections six logical pixels away from their
cells and clipped row numbers. The document inventory gives spare width to the
document name, sizes status columns to their labels, hides row numbers and uses
46-pixel rows with subtle separators. Full names remain available in tooltips.

The document list takes the available height. Common actions wrap at narrow
widths, selection counts and enabled actions reflect single/multiple selection,
and deletion options expand on demand. Empty result panels are hidden. Deletion
still requires a current preview; changing selection or retention options clears
that preview and disables confirmation.

Run the populated-table acceptance with a new output directory:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m openkb.desktop.verification \
  --tables --output /tmp/urltrakb-tables
.venv/bin/pytest -q tests/test_desktop_workbench.py
```

The tests cover light/dark themes at 1320×720, 900×650 and 720×600, repeat at
150% device scaling, and check header/cell geometry, scrolling, full-name tooltips,
selection, empty lists and actual deletion with stale-preview rejection.
These captures use synthetic source names in an isolated KB, rendered by Qt on
Linux; they do not certify the Windows portable build.

| View | Native capture |
| --- | --- |
| Documents with a selection, light | [Document table](desktop-evidence/workbench/documents-table.png) |
| Documents, dark, narrow window | [Narrow document table](desktop-evidence/workbench/documents-table-dark.png) |
