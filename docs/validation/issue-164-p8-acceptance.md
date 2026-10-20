# Issue 164: P8 native storage acceptance

Date: 2026-10-20. Implementation: `98a4cee1` on `dev-1.2.0`.
Review base: `b727b54d4478ecf0e0ffa2ffdab521868846bdbd`.
Status: required validation gates passed.

## Delivered behavior

New knowledge bases and all three entry points use the shared SQLite/CAS
application chain. The compiler retains accepted summaries, plans, updates,
indexes, usage and budgets independently of publication. Readers bind complete
immutable publications, source/processing revisions and image bytes. The former
file registries, private index databases, JSON task/usage ledgers and publication
snapshot readers have been removed. V1 knowledge bases are refused before
writes or model sends; no migration or dual write is provided.

Conversations, generator artifacts, source and worksheet maintenance, candidate
review/disposal, bounded task details, explicit GC and verified backup/restore
use that same authority. Restored generation work reuses accepted paid results
under a new execution identity and the existing cumulative budget. Restored
artifact copies are recreated from retained master bytes. Empty recovered HTML
has an explicit zero-send terminal receipt. Office/CNKI provenance and desktop
original/PDF opening use the exact retained processing revision.

## Verification

| Gate | Result |
| --- | --- |
| Final offline full inventory | 3,846 passed, 16 skipped; 561 live cases deselected |
| Exact staged-source regression, excluding unrelated user edits | 117 passed; 1 live test deselected |
| Desktop source-file/entry-point and related format matrix | 11 passed, 2 skipped; 30 live tests deselected |
| New source-file, application-boundary and module-size matrix | 7 passed |
| Ruff check / format check | Passed |
| Mypy | 570 source files, no issues |
| Frozen lockfile check | Passed with unchanged pinned dependencies |
| Removed-module/export reference audit | No remaining references |
| Standards / Spec static review | 0 / 0 P0/P1/P2 findings |

The final frozen-code run was externally interrupted after 3,331 reported test
results (3,315 passed and 16 skipped). The same collection contains 3,862 selected
cases. Its final boundary case was rechecked successfully; the remaining 531
cases were selected from that inventory and all passed. The result is
therefore the complete inventory across resumed segments, not a claim of one
uninterrupted final pytest invocation. The preceding uninterrupted full run
passes 3,845 cases with 16 skipped and 561 live cases deselected.

The main offline suite uses actual SQLite/CAS, the pinned SDKs and loopback HTTP
for deterministic model responses. Real crash/worker and Qt cases remain in the
matrix. Live provider tests are deliberately selected separately; deselected
live cases are not reported as executed. Replaced private-file implementation
assertions and their native behavior coverage are mapped in
[storage-cutover-tests.md](../storage-cutover-tests.md).

The final real-provider short-PDF import passes its single selected test.
Across all attempts of this final small-sample gate, the single guarded ledger
records 18 actual sends, all settled: CNY 0.08579008 from returned token usage, with
CNY 0.17158016 conservative guarded exposure under the CNY 2 limit. This is
usage-based accounting, not a provider invoice. No other live matrix is claimed.

## Build and runtime

The wheel and source distribution were built from a clean archive of
`98a4cee1`, excluding unrelated working-tree edits. The archive version is
`1.2.3.dev203+g98a4cee1`, derived from tag `v1.2.2`, commit count and SHA.
All 570 tracked application Python files in the wheel equal that commit;
42 removed Python modules are absent. SDK wheels, source archives, licenses,
provenance and 2,937 retained vendor-source files pass byte verification.

An isolated installation imports all application/SDK modules from its own
site-packages and passes the dependency check. Actual SDK loopback compilation
sends two requests, publishes retained knowledge after external input removal,
and reads a persisted conversation. The actual linked SQLite is 3.53.1.
PyInstaller 6.22.2 collects the four SDKs' data, metadata, ConDB prompts/config,
LiteLLM model table and application prompt/template resources.

An actual Linux PyInstaller executable creates and opens a native store from
a Qt application, then launches its own frozen executable as an independent
worker. Both pass `require_sqlite()` with SQLite 3.53.1 and the required binding
configuration API; the worker validates the original database generation.
This probe and resource collection are not a complete desktop installer release
or a claim that Windows/macOS installer or subscription-login checks ran.

Wheel SHA256:
`02c1ce2e6efa7402be768527a4da6a071c215722df59dbdd9e325af786a32a3f`.
Source distribution SHA256:
`b200de406bca69e72ec9b62f9fd4a118e5e38c97c000679c391ae4c4a0f687cd`.

## Performance evidence

The earlier measured protocol gates remain available, with their actual
workload boundaries and limitations:

- [Usage callback scaling and full aggregate audit](issue-164-usage-benchmark.md):
  1,000 / 5,000 / 20,000 requests, zero aggregate differences or writer timeouts.
- [One hundred revisions and complete compiler baseline](issue-164-revisions-benchmark.md):
  one retained copy of stable binary content; no unrelated attachments/index
  copies; complete Markdown preparation performs zero baseline fsync calls.
- [Conversation snapshot, projection and legacy parity](issue-164-conversations-benchmark.md):
  original-message restoration, accepted-prefix reuse and bounded final projection,
  including the separately measured legacy comparison.
- [Actual GC/read/backup/model-wait leases](issue-164-maintenance-benchmark.md):
  busy means zero deletion during active reads/backup; pure model waits permit
  unrelated GC; explicit tree retirement remains bounded.
- [Optional observation overhead](issue-164-observations-benchmark.md):
  actual XLSX parsing median overhead 1.20% on the recorded host/workload,
  with the core ledger always enabled.

These are single-host measurements, not latency guarantees, physical-space
claims derived from logical byte totals, or linear long-conversation complexity.

## Standards

Final static follow-up reports no P0/P1/P2 findings. It confirms shared application
ownership, fixed database/unit identities, cancellable GC access and background
resource transfer. Temporary source-file cleanup uses only the owned
`TemporaryDirectory` handle. These final follow-up reviews used text-only reads.

## Spec

Final static follow-up reports no P0/P1/P2 findings. It confirms exact original
and converted-PDF binding, new-generation restoration without paid resends,
terminal non-import outcomes and suppression of stale desktop callbacks.

Findings: Standards 0; Spec 0.
