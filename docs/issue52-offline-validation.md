# DocumentPlan implementation: offline validation and handoff

This record covers the local implementation of #53–#63 under #52. It does **not** claim
#64's real-model semantic acceptance. During this development run the user explicitly
requested no online validation; model behavior was exercised only through the local,
controllable HTTP fixture. Historic provider experiments in other documents remain
historic observations, not evidence for this implementation.

## Reproduce the offline gates

From the repository root, with the pinned development environment installed:

```bash
uv run pytest -q tests/test_navigation_windows.py tests/test_document_orchestrator.py tests/test_document_pages.py tests/test_document_compilation.py tests/test_document_measurement.py
uv run ruff check .
uv run ruff format --check .
uv run mypy openkb
uv run pytest -q
```

## Recorded offline gates (2026-09-23)

On `dev-1.1.0` after the two-axis review fixes, the focused document,
concurrency, diagnostics and file-size set reported **152 passed, 1 skipped**.
The complete suite reported **2668 passed, 2 skipped** in 16m14s. `ruff check .`,
`ruff format --check .`, `mypy openkb`, and the module-size gate passed.
Three non-failing warnings came from LiteLLM's logging-worker coroutine in
`tests/test_provider_usage.py`. No online model request was made.

The review identified and resolved three offline issues: ledger persistence
and proof digests now share canonical Unicode/key-order JSON serialization;
window scheduling and the planning protocol reject the same malformed range
shapes; and the planning ledger closes on normal return and exceptions.

The focused tests cover a second-step request **above 256,000 input tokens** without
exceeding its configured 512,000-token model context; 1M/10M-token descriptor-scale
window scheduling and durable restart without materializing source bodies; and a
controlled import whose physical calls are planning, generation and verification,
with no facts request. The shared-pool test holds two admissions concurrently and
asserts the observed 240-token reservation peak, rather than mistaking its
500-token configured capacity for the peak. The import test checks one accepted
window, source-bound page receipts, actual provider usage, group attribution,
first inspectable result time and schema-4 aggregates. Legacy schema-3 usage
remains readable.

`usage.measurement.document_planning` contains source-free per-window W/S/T
observations, including target/completed original ranges, carry counts, frozen
prefix hash/size, accepted checkpoint/result identities, local adoption and
provider usage when returned. `usage.measurement.requests` records actual HTTP
attempts, model options and provider usage. `usage.measurement.summary` contains
request latency p50/p95, RSS and atomic in-flight reservation peaks, token totals,
and document/group timing. Missing provider usage and monetary cost are `null`,
not zero; an in-flight reservation is not billed usage. The first inspectable
timestamp is taken only after an accepted/adopted plan observation is recorded.

## Manual semantic acceptance still required for #64

Before any real request, the reviewer should record the effective model and
endpoint, a redacted configuration snapshot, source and parse identities, the
remaining cumulative request/token/time/currency allowance, and the exact code
commit. Use an isolated knowledge base and unchanged originals; preserve full
private request/response bodies only in the authorized private location. Stop if
the allowance or source identity cannot be verified.

Run the recovery-plan source and representative three-window, dense-condition,
cross-domain and long-body sources through formal `import_document` and
`continue_source`. Check UUID conditions, distinct recovery branches, explicit
backup citations, unresolved external dependencies, late-source coverage and
attachment storage without hidden model tasks. Review overview, page plan,
original-range routes, generated candidates, critical-review verdicts,
omissions, publication and continuation receipts. Keep `none` drafts separate.

With quality criteria fixed in advance, compare semantically complete W first,
then concurrency 1/2/4 using the same source identity and effective model.
Report actual attempts, retries, cache categories, provider usage, billed cost
when available, wall time, first inspectable result, p50/p95, RSS/in-flight
peaks and range carry. Do not infer provider cache hits from local adoption or
claim a speedup from incomplete or unmatched runs. Only after this review passes
may #64 be closed as real semantic acceptance; #52 is a parent specification
and is not changed by this handoff.
