# Issue 164: actual usage storage calibration

Measured 2026-10-19 on Python 3.12.13, SQLite 3.53.1, Linux 6.12.111 x86_64 /
glibc 2.41. [Exact machine-readable results](issue-164-usage-benchmark.json)
include every reported distribution and its sample count.

Reproduce from the repository root:

```bash
.venv/bin/python scripts/verify_storage_usage.py --output /path/to/usage-results.json
```

The script creates and removes only its own temporary KB beneath the output
directory. It uses the real application execution scope, SQLite/CAS, ten admitted
sources/units, four stage codes and actual UsageStore admission/receipt APIs.
Requests and known-unsent outcomes are synthetic: no SDK sends, network calls,
provider usage or billing are claimed. Every callback commits one actual writer
transaction. The first 50 request pairs are excluded; callback distributions at
each checkpoint include all subsequent samples up to that point. Queries have
seven samples per checkpoint. The host was not isolated; focused checks also ran
near the beginning, so the recorded maximum must not be interpreted as a pure
storage lower bound.

| Requests | Callback median / p95 / max (ms) | Writer hold p95 / max (ms) | Writer wait p95 / max (ms) | WAL bytes |
| --- | --- | --- | --- | --- |
| 1,000 | 4.990 / 5.828 / 103.601 | 0.894 / 99.089 | 0.002675 / 0.008054 | 4,181,832 |
| 5,000 | 4.880 / 5.371 / 103.601 | 0.808 / 99.089 | 0.002365 / 0.021687 | 4,206,552 |
| 20,000 | 5.021 / 8.044 / 103.601 | 3.622 / 99.089 | 0.002385 / 0.021687 | 4,206,552 |

| Requests | Task list median (ms) | Source summary median (ms) | Explicit full audit median (ms) | Main DB bytes |
| --- | --- | --- | --- | --- |
| 1,000 | 5.760 | 4.223 | 16.840 | 2,682,880 |
| 5,000 | 5.865 | 4.187 | 64.991 | 9,203,712 |
| 20,000 | 5.955 | 4.184 | 245.278 | 33,722,368 |

All three checkpoints reconstruct the full ledger and compare every aggregate
and stage counter without differences. The number of counter rows stays 130;
actual sends and budget consumed remain zero. No callback writer error, rollback
or timeout occurs. Ordinary callback SQL changes the request and related buckets;
only explicit audit traverses the ledger. Similar medians are evidence for this
workload, not a constant-time promise: checkpoint work and B-tree growth still
appear in tail latency.

The pre-fix full calibration was interrupted after its 1,000-request checkpoint
because connection-open cost and WAL growth prevented timely completion; it has
no completed 5k/20k result. A separate unprofiled 200-pair differential retained
one idle connection as its only changed variable: elapsed 4.578 to 1.950 seconds,
WAL 14,127,512 to 4,152,992 bytes. This led to retaining a local idle read-only
connection within both existing application operation scopes. Formal writes
still use FULL durability and their existing transactions. The connection holds
no SQL snapshot or reclamation lease and stays out of worker serialization.

Real application regressions first reproduce WAL growth for both scopes, then
verify reuse near the default checkpoint threshold and GC availability between
callbacks. Actual SDK model-wait GC and fixed-read tests also pass. SQLite
documents both [automatic WAL checkpointing](https://www.sqlite.org/wal.html#automatic_checkpoint)
and the [client-close checkpoint control](https://www.sqlite.org/c3ref/c_dbconfig_defensive.html#sqlitedbconfignockptonclose).
The observed lifetime effect is an experimental finding; those documents alone
do not prove its internal cause.

These results cover request storage and explicit aggregate audit. They do not
replace the large-tree/receipt contention, compiler materialization, continuous
conversation, 100-revision deduplication or final P8 delivery gates.
