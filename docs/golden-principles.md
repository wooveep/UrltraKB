# Golden Principles

Opinionated, mechanical rules that keep this agent-generated codebase legible
and consistent for future agent runs. Enforced by CI where possible; the rest
are honored by convention and checked in review. When a rule proves valuable,
promote it into a lint (see `tests/test_file_size.py` for the pattern).

## Boundaries
- **Validate data shapes at boundaries.** Parse/validate inputs (frontmatter via
  `openkb/frontmatter.py`, config via `openkb/config.py`) at the edge. Never build
  on guessed shapes.

## Reuse
- **Prefer shared utilities over hand-rolled helpers** so invariants stay
  centralized. Check `openkb/` for an existing helper before writing a new one.
- **CLI, desktop and HTTP adapters call the same `openkb/application/` use cases.**
  The desktop is the behavior baseline. Adapters validate requests, schedule work
  and present results; model selection, final-answer handling, persistence,
  archive policy and KB leases belong to the shared use case. Application code
  must not import entrypoint adapters (`tests/test_application_boundaries.py`).
- **Compose pipelines with ordinary functions and explicit options.** Keep the
  sequence in the application operation; add no plugin registry or configurable
  pipeline engine without a concrete requirement.
- **Capture execution settings once, after acquiring the KB lease.** Batch
  adapters reuse one `ExecutionContext`; a fresh operation captures fresh settings.
  Shared-use-case adapters do not resolve credentials or mutate process model settings.
- **Verify behavior across adapters at the model or storage boundary.**
  `tests/test_entrypoint_consistency.py` compares retained answers, snapshots,
  session scope and artifact replacement/export. HTTP cancellation additionally
  waits for model cleanup before releasing leases (`tests/test_answer_streams.py`).

## I/O and state
- **All wiki file writes go through `openkb/locks.py` / `openkb/mutation.py`**
  (atomic, crash-safe). No ad-hoc writes to the wiki tree.
- **Log through `openkb/log.py`**, not bare `print`, for anything diagnostic.

## Size and shape
<a id="file-size"></a>
- **Keep modules focused and under 800 lines** (enforced by
  `tests/test_file_size.py`). Split large modules into focused units by
  responsibility. Existing over-limit files are grandfathered (with reasons)
  in the test's `_GRANDFATHERED` set and additionally tracked in
  `docs/internal/tech-debt.md` *(maintainer-local, not in git)*.

## Docs
- **`AGENTS.md` is a map, not a manual.** Keep it short; deep/local docs live
  under `docs/` (public) and `docs/internal/` (maintainer-local, not in git).
