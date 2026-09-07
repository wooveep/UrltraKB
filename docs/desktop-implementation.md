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
