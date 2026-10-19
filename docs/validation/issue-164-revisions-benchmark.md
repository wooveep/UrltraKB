# Issue 164: revision deduplication and compiler baseline measurement

Measured on 2026-10-19 with Python 3.12.13 / SQLite 3.53.1 on Linux x86_64.
The final run followed the focused regression run; no other task test or type
checker was deliberately running concurrently. This is one host and one sample,
not an isolated hardware benchmark. Public generated fixtures contain no user
documents, credentials or model responses. No model request was sent.

Reproduce with:

```bash
.venv/bin/python scripts/verify_storage_revisions.py --output /absolute/owned/output.json
```

The script uses the real non-model application execution scope, catalog,
SQLite/CAS store, knowledge revision builder, navigation and compiler baseline
materializer. Every assertion checks actual storage or copied bytes. Filesystem
instrumentation delegates to the real fsync implementation. Temporary cleanup is
restricted to the unique directory created by `TemporaryDirectory` beside the
chosen output file.

## One hundred additional body revisions

The initial publication contains 64 stable Markdown pages plus policy/index,
a valid 3,147,775-byte PNG, a 16,777,216-byte unrelated attachment and an excluded
opaque index fixture. A 4,160,020-byte admitted original is separately retained
by the catalog. Each of 100 subsequent publications changes only `index.md`.

| Measurement | Initial | After 100 additional revisions |
| --- | ---: | ---: |
| Knowledge revisions | 1 | 101 |
| Registered CAS objects | 74 | 474 |
| CAS file bytes | 28,850,269 | 30,326,453 |
| CAS allocated bytes (`st_blocks × 512`) | 28,897,280 | 31,354,880 |
| Blob reference rows | 143 | 14,243 |
| Main database file bytes | 1,081,344 | 12,009,472 |
| WAL bytes at observation | 1,339,032 | 4,824,552 |
| Copies of each stable original/image/attachment digest | 1 | 1 |

The 100 preparation/publication operations took 6.036 seconds. CAS growth was
1,476,184 file bytes from changed content and revision/navigation/sync manifests;
SQL rows and database storage still grow with retained immutable history. The
script verifies physical CAS names match registered objects, only the single
main SQLite file exists, and no legacy knowledge/snapshot/staging/proposal
directory is created. It reads the original after deleting its external file,
the initial publication after all changes, and every fixed binary payload.

## Complete compiler Markdown baseline

The fixed baseline exposes the entire publication manifest, materializes every
Markdown file plus the explicitly required PNG, and compares their bytes with
the input publication:

| Measurement | Observed |
| --- | ---: |
| Markdown files / bytes | 66 / 4,170,396 |
| Required attachment bytes | 3,147,775 |
| Unrelated attachment / index fixture bytes copied | 0 / 0 |
| Total baseline bytes copied | 7,318,171 |
| Whole publication logical bytes | 24,675,387 |
| Baseline preparation | 13.812 ms |
| Baseline fsync calls | 0 |

The 17,357,216-byte selection difference is logical copying avoided in this
fixture, not a measured physical-space saving or a comparison with an old
compiler's speed. This interval covers fixed baseline materialization, not
source parsing, additional source-input materialization, model generation or
publication synchronization. The script verifies GC can acquire its lease after
materialization and private edits cannot change CAS; the private directory is
removed on scope exit.

After all workload assertions pass, the real application scope records success;
the script reads the durable execution state back and verifies `completed`.

The related real compiler/workspace/figure/response-recovery/boundary/size matrix
passes 40 tests. Those regressions exercise actual compiler algorithms and SDK
loopback responses; this no-model measurement does not substitute for their
content and recovery checks. Long-chat calibration, remaining root retirement
and the sole public P8 activation remain separate unfinished gates.
