# Issue 164: v2 storage protocol development

Status: **development foundation only; issue 164 is not complete and must stay open.**
The CLI, desktop, HTTP, compiler and existing knowledge bases still use the
previous storage implementation. No public creation path enables format 2.
These modules are the new-format test factory permitted before joint integration
by [issue 164](https://github.com/wooveep/UrltraKB/issues/164), not a migration or
compatibility reader.

## Implemented interfaces

`ultrakb.storage.database.create_database` explicitly creates an empty v2 store.
`open_database` opens only an existing store and validates its format, schema,
writer, capabilities and optional exact KB/generation identity before writer
configuration. The linked SQLite library must include the WAL-reset fix:
3.51.3 or later, or the documented 3.44.6 / 3.50.7 backport series. Creation is
serialized by the existing external lifecycle lease. Connections remain owned
by their creating thread. Durable transactions are top-level, use `FULL`
synchronization and reject nesting, including nesting through another connection
in the same execution context. Opening and closing never initialize schema or
run an integrity scan/checkpoint.
Active databases must already be in WAL mode; opening does not silently repair
their journal mode. Only explicit sealed archive reads use `immutable=1`, and
they reject a nonempty WAL. Restore explicitly establishes active WAL mode.
Active connections disable SQLite's last-close checkpoint using
`SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE`. The development format therefore also requires
bindings exposing `setconfig`/`getconfig` (available in the tested Python 3.12
runtime); an unavailable binding is rejected before storage creation. This gate
must also be checked in each frozen/worker target before public enablement.
Creation and restore explicitly establish persistent WAL/SHM coordination files.
An active store missing either sidecar is refused for explicit recovery before
opening SQLite, so a rejected format cannot create these files as a side effect.
Recreating missing sidecars is reserved for an explicit maintenance operation,
not ordinary reading; that application repair entry point remains outstanding.

`CatalogStore.admit` gives each source its own identity and retains the original
file as an immutable CAS Blob. The input manifest contains logical relative
paths, byte counts, kinds and digests. `read_original` does not consult the
external input path. The current factory accepts one main file; sidecar,
normalization, processing-unit and pending-job admission still need integration.

`KnowledgeStore.prepare_revision` prepares content and manifest Blobs, commits
bounded draft batches, seals, validates outside a write transaction, and marks
the exact sealed object ready. `publish` checks the expected publication and
atomically advances the public head. `bind` returns immutable publication reads;
later publications do not change their content or images. Published history is
retained. The ready-navigation, source-target, import-attempt and projection-plan
bindings required for production publication are not implemented here yet.

`SessionStore` retains complete immutable exchanges. Each accepted CTree
checkpoint is independently committed. `session_codec` saves every SDK snapshot
field except `turns`, retaining exact conversation/policy/prefix identities.
Restore loads the authoritative prefix, validates identities, message roles and
content, and calls the actual SDK `CTree.restore_state`. This does not change
ChatIndex algorithms. Current retrieval projection bindings, catch-up batching
and preparation's recall gating still need to be connected to this store.

`Maintenance.preview` reports a bounded candidate window and protected Blob
totals. Explicit `collect` rechecks current references under a nonblocking
exclusive reclaim lease. Busy means zero deletion. Retirement is committed
before unlink; interruption leaves a resumable retiring record. New references
to retiring content are rejected. File removal is outside SQL transactions.
The current collector handles registered unreferenced Blobs; orphan discovery,
checkpoint/root retirement, tree deletion, observation retention and unconfirmed
receipt recovery are still required.

Online `backup` holds a shared reclaim lease before the SQLite Online Backup API
snapshot and through all referenced Blob copies. Copies are independent,
digest-checked files. `restore` validates the database, copies referenced content
and establishes a new database generation. Backup coverage explicitly reports
that configuration, working-copy differences and external receipts are absent;
these development backups are not advertised as complete application recovery
packages. Application task interruption and lifecycle rebinding remain work for
the integration stage.
The exported snapshot drops unreferenced Blob metadata in bounded batches,
matching the orphan files it omits; the live database is unchanged. Reference
closure verification checks both content digests and byte counts.

## Physical records and owners

SQL and transaction ordering remain inside `ultrakb/storage`. None of these
tables is a public arbitrary-table CRUD interface.

| Records | Lifecycle, constraints and actual caller |
| --- | --- |
| `ukb_meta`, `ukb_schema_versions` | One explicit storage identity and schema authority; creation/open guard. |
| `ukb_blobs` | Digest-unique immutable payload metadata; live/retiring state; CAS and maintenance. |
| `ukb_blob_refs` | Indexed actual references owned by business stores; reference and owner commit together; retiring content rejects insert/update. |
| `ukb_sources`, `ukb_source_revisions` | Distinct source identity and ordered retained originals; catalog admission/read. |
| `ukb_conversations`, `ukb_turns` | Conversation identity and complete immutable exchanges; ordinal, turn and attempt uniqueness. |
| `ukb_conversation_checkpoints`, `ukb_session_heads` | Independently accepted exact prefixes and latest calculation root per policy; session save/restore. |
| `ukb_knowledge_revisions`, `ukb_revision_files` | Draft/sealed/ready/published manifests; insert only into draft, no file updates; revision preparation. |
| `ukb_publications`, `ukb_heads` | Retained publication identity and expected-version public head; publication/bound reads. |

The generic Blob reference index is not itself proof that an owner remains
live. Business-store retirement must remove its reference rows in the same
transaction. No owner-retirement API is exposed until that lifecycle is complete.

## Lock and durability boundaries

The intended order remains lifecycle → session when needed → storage-stage
reclaim → projection coordination when needed → KB write coordination when
needed → short SQL transaction. `storage_stage` acquires a real shared lease
using the existing `_Lease` implementation and validates the expected database
identity after acquisition. It does not hold a database read transaction.
Collection does not take a session lock or upgrade a shared lease.

Current source revisions, immutable turns, accepted checkpoints and knowledge
revisions retain their Blob references across stages. This factory conservatively
retains all such records. Latest-computation and current-projection roots must be
distinguished before enabling intermediate-checkpoint retirement. Model waits
must occur outside storage stages when application integration is added; no
current production model-wait optimization is claimed by these witnesses.

CAS writes validate bytes, synchronize the payload, atomically rename it and
synchronize its containing directories before committing references. Wiki and
CAS files are never hard-linked. This work does not weaken existing atomic
copy/write behavior. The private compile-materialization helper and its separate
temporary durability rules are still outstanding.
An existing unregistered CAS pathname can represent an interrupted directory
sync. Reuse durably rewrites it before registering metadata. Read-only stages
reject content acceptance before any filesystem mutation.

## Development calibration

Measured on 2026-10-18, Python 3.12.13, SQLite 3.53.1,
Linux 6.12.111 x86_64 / glibc 2.41, temporary local storage. Two threads used
separate connections: 100 batches of 128 rows with 512-byte payloads and 200
single-row commits. Wait includes entry validation and `BEGIN IMMEDIATE`;
held time is from transaction entry through durable commit. No network or model
was involved. This SQL witness is not a representative ConDB tree benchmark.

| Workload | Wait p95 / maximum | Held p95 / maximum | Failures |
| --- | --- | --- | --- |
| 128-row batches, 100 samples | 0.010 / 1.083 ms | 0.137 / 0.990 ms | 0 |
| Independent single rows, 200 samples | 0.011 / 18.272 ms | 0.008 / 0.671 ms | 0 |

Initial development limits are 128 manifest write rows, 256 KiB encoded entries
per batch, 64 KiB per entry, and 2,000 ms SQLite busy timeout. Manifest entry
preparation/serialization and Blob I/O happen before their write transactions.
GC defaults to at most 128 registered candidates / 64 MiB per invocation.
These are implementation bounds, not a promise that large trees or all
application transactions meet an elapsed-time target. The remaining P0/P3
calibration must use representative large trees, queries and actual usage
receipts, including WAL size, p95/maximum lock time and timeout coverage.

## Verification and remaining gates

The focused `test_storage_*` tests cover format refusal, unsafe runtime refusal,
independent rollback boundaries, source identity/deduplication, external-source
deletion, real CTree restoration and tamper refusal, fixed publication reads and
conflicts, GC reference rechecks/busy, interrupted retirement and backup restore
generation changes. `test_cross_platform_locks` covers the reused lock protocol.

Recorded checks on the development foundation:

- Full default offline suite: **3,407 passed, 14 skipped, 745 live tests
  deselected**, two upstream WebSocket deprecation warnings, 504.58 seconds.
  The three final WAL regressions were added during that run and are included
  in the separate final focused run below.
- Final focused storage suite: **28 passed** against the final code.
- `ruff check .`, `ruff format --check .` (920 files) and `mypy ultrakb`
  (451 source files) passed.
- Independent Standards and Spec reviews found and verified corrections for
  reserved reference-slot collisions, read-only acceptance side effects,
  unconfirmed CAS rename durability, omitted orphan metadata in backups,
  restored journal mode, format-probe side effects and last-close checkpointing.
  Their final foundation reviews reported no remaining verified code defects.
- The original uncommitted tracked diff was compared byte-for-byte with the
  saved pre-task diff and remained unchanged. Existing untracked work was not
  included in the storage commits.

These results validate the development foundation only. Both reviews retain
the overall specification's incomplete status. No live model or complete
three-entrypoint / packaged-runtime acceptance is claimed.

Before issue closure, implement and verify the remaining P0–P8 work from the
issue, in particular:

- ConDB borrowed connections, tree-local entities, bounded sealed tree builds
  and retirement; document/index revision and ready-navigation bindings.
- Durable model budgets, send/response receipts, compile/index checkpoints,
  incremental authoritative usage, stages, tasks and paginated task details.
- Joint writer/reader/session/configuration integration through shared
  application use cases; fixed source/image resolvers and generation checks.
- Persistent Wiki projection plans, application edit preconditions and external
  editing pause/accept/discard; full Markdown/necessary-attachment preparation.
- Demand-driven ChatIndex catch-up with one final projection, distinct computed
  and searchable prefixes, and both roots protected by maintenance.
- Full recovery/retention/backup coverage and the issue's fault/performance
  matrices, SDK provenance/package checks, final real-model samples, then P8
  public enablement and removal of the previous storage paths.

Passing these isolated foundation tests cannot substitute for those gates or
justify closing the total specification.
