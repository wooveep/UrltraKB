# Issue 164: actual reclaim leases and maintenance batches

Measured on 2026-10-19 with Python 3.12.13 and SQLite 3.53.1, on the workspace's
btrfs filesystem. The implementation baseline was `9cd01b30`. Reproduce from the
source checkout with:

```sh
.venv/bin/python -m scripts.verify_storage_maintenance --nodes 5000 --output /existing/owned/directory/result.json
```

The runner creates one temporary synthetic KB under the output parent, isolates
its global settings, and removes only that owned temporary directory. It uses
actual shared application maintenance and question use cases, SQLite/CAS,
ConDB, and the real model SDK with a loopback HTTP provider. There are no paid
model requests or user KB inputs.

## Measurement boundaries

The instrumentation delegates every `_Lease.try_acquire` and `release` to the
real implementation. Only an outer OS acquisition of this KB's `reclaim.lock`
is timed, from successful acquisition return to completed release. Nested
reentrant calls are excluded. Context variables identify phases across actual
async tasks and storage threads; no connection or lock handle is transferred.
The interval includes the small instrumentation bookkeeping overhead. It is a
reclaim lease interval, not a SQL write transaction duration or task wall time.

The 5,000-node document tree has no retained owner, plus eight small orphan
Blobs. Each explicit shared-use-case GC call uses `max_items=1` and a 64 MiB
byte limit; the existing tree deletion work limit still applies within each
call. Preview runs between batches, outside the timed GC call. The report
contains separate batch wall and exclusive lease distributions:

| Measurement | Samples | Median | p95 | Maximum |
| --- | ---: | ---: | ---: | ---: |
| GC exclusive reclaim lease | 169 | 9.71 ms | 15.58 ms | 69.85 ms |
| Complete GC application call | 169 | 21.63 ms | 28.82 ms | 81.47 ms |

All 169 calls completed, with zero busy results in this sequential workload.
They removed one tree and ten Blobs totaling 12,999,242 logical bytes, with at
most 87 tree rows removed per batch. No candidates remained. The final WAL was
4,239,512 bytes. These are single-host measurements, not latency guarantees or
evidence of starvation freedom under continuous storage traffic.

## Actual access and model wait witnesses

- A fixed published source read pauses at its actual Blob file read boundary.
  GC returns `busy` and deletes nothing. Its shared reclaim interval was
  11.11 ms, including the controlled pause and concurrent GC attempt.
- An online backup pauses at its actual CAS copy boundary. GC returns `busy`
  and deletes nothing. Its shared interval was 89.16 ms, including the
  controlled pause, online database snapshot, and verified Blob copies.
  Restoring the package into another directory reads the same original bytes.
- An actual `ask_question` waits for its loopback HTTP response. At that point
  no question reclaim lease is active; GC acquires exclusive protection and
  removes a specifically seeded unrelated orphan. The answer then completes
  with one actual SDK send. The GC lease interval was 12.14 ms.

The controlled read/backup durations are protection witnesses, not read or
backup throughput results. The question still takes 28 short shared storage
stages around preparation, receipts, and retained results (maximum 116.84 ms).
Building the real document tree is a complete storage stage (904.24 ms).
Actual long I/O and tree queries can therefore still make GC busy; the change
only releases protection across waits without storage access.

The original fixed publication remained readable after collection. Database
integrity and foreign-key checks passed. The complete machine-readable result
is in [issue-164-maintenance-benchmark.json](issue-164-maintenance-benchmark.json).
