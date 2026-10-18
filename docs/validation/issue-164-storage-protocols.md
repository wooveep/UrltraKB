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
external input path. Processing versions retain full output/sidecar manifests,
an explicit normalized-source path/protocol, and stable processing-unit targets.
These interfaces still need application admission integration.

`KnowledgeStore.prepare_revision` prepares content and manifest Blobs, commits
bounded draft batches, seals, validates outside a write transaction, and marks
the exact sealed object ready. `publish` checks the expected publication and
atomically advances the public head. `bind` returns immutable publication reads;
later publications do not change their content or images. Published history is
retained. Every candidate prebuilds and retains ready deterministic navigation,
including an explicit empty tree. The exact sealed Wiki synchronization plan
activates in the same short publication transaction. Source-target and
import-attempt bindings required for production publication remain outstanding.

`SessionStore` retains complete immutable exchanges. Each accepted CTree
checkpoint is independently committed. `session_codec` saves every SDK snapshot
field except `turns`, retaining exact conversation/policy/prefix identities.
Restore loads the authoritative prefix, validates identities, message roles and
content, and calls the actual SDK `CTree.restore_state`. This does not change
ChatIndex algorithms. The development application `ConversationMemory` now
checkpoints every accepted update, constructs at most one final retrieval tree
per catch-up and atomically binds it to the exact checkpoint. Current computation
and searchable projection roots are distinct; a failed tree commit returns the
old tree's real coverage, and the next call reuses accepted computation. A failed
last checkpoint reloads the preceding durable prefix before export. Preparation
checks the existing 8,000-token threshold, recall budget and candidates outside
the six recent turns before indexing. An unchanged complete prefix performs no
encoding or tree write. Model waits hold the session/lifecycle lease without a
storage stage; cancellation is checked while waiting for the session lock and
after checkpoint Blob synchronization. All original roles remain restorable;
automatic analysis/export uses only users. The 17 focused SDK/session tests
include SQL trigger failures, stage-budget continuation and cancellation races.
Production question/session adapters still need joint integration. Intermediate
checkpoint retirement and full long-session cost measurements remain outstanding.

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
copy/write behavior. `BoundKnowledge.compile_workspace` materializes complete
Markdown plus explicit required attachments through the controlled
`locks.PrivateMaterialization` helper. Targets are fresh private temporary
directories; streamed copies verify digest and byte count in one pass without
fsync, index copies or whole-KB rollback snapshots. It releases the database
connection and reclaim stage before yielding compiler input. Fixed publication
roots retain the source Blobs. Tests verify selectors, exact old-version reads,
independent writable files, GC during a model-wait equivalent, cleanup and a
zero-fsync copy alongside continuing fsync for durable content. Publication
paths reject nonportable colon names before accepting any content. Existing
compiler/application callers still need to use this development entry point.
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

### Catalog, pending work and independent execution receipts

The next development batch retains complete input/sidecar manifests, accepted
processing outputs and stable processing-unit targets. Identical source bytes
and sidecars reuse the accepted input revision; processing fingerprints and
worksheet keys remain independent of source content and queue ordinals.
Persistent import intents retain the root budget and all previous attempts when
retried, and reject changed source/unit targets before work starts.

Execution records and model budgets precede requests. Receipt fields merge by
their own observation versions, with unknown values preserved and sends never
reverted to unsent. Indexed counter deltas commit with each request update;
normal receipt callbacks do not scan the ledger. Accepted compile steps and
received response bodies use independent CAS/reference commits. SQL failure or
interrupted Blob retirement preserves a durable pending response packet, which
explicit repair accepts without reserving another send.

The focused storage suite now passes **39 tests**, including publication failure,
repeated/late partial usage, unchanged retry budgets, interrupted retirement,
response repair, and restored-generation rejection of old request/dispatch
identities. `mypy ultrakb/storage` and focused ruff checks pass. Standards review
identified the retirement fallback and shared-unlink utility correction; Spec
review additionally identified restored callback generations and the need for
an explicit request-to-stage-attempt binding. These corrections are included.

This is still a development store: existing compiler checkpoint recovery modes,
worker/application wiring, complete execution recovery and cross-entrypoint
task details remain integration gates. The import discovery budget snapshot is
durable, but discovery metering is not yet connected to this new queue.

Before issue closure, implement and verify the remaining P0–P8 work from the
issue, in particular:

- Representative tree/ledger calibration and complete publication preconditions
  beyond the development ConDB, index and ready-navigation interfaces.
- Complete model-budget/receipt and compile/index-checkpoint integration,
  incremental usage audit, stages, tasks and paginated task details.
- Joint writer/reader/session/configuration integration through shared
  application use cases; fixed source/image resolvers and generation checks.
- Joint application writer/reader adoption of persistent Wiki projection and
  external editing protocols; full Markdown/necessary-attachment preparation.
- Demand-driven ChatIndex catch-up with one final projection, distinct computed
  and searchable prefixes, and both roots protected by maintenance.
- Full recovery/retention/backup coverage and the issue's fault/performance
  matrices, SDK provenance/package checks, final real-model samples, then P8
  public enablement and removal of the previous storage paths.

Passing these isolated foundation tests cannot substitute for those gates or
justify closing the total specification.

### Shared ConDB trees and document index revisions

The development schema now embeds ConDB schema 2. Borrowed SDK connections
preserve the owner's transactions, row factory and PRAGMAs. Entity identities
and every entity join are scoped to their tree. Prepared actual SDK rows commit
in batches capped at 63 items and 256 KiB, including content-reference rows.
Large entity and node-attribute JSON lives in CAS and is resolved by the SDK;
the complete original document, pages and source locators remain readable.
Draft rows cannot be moved into a sealed or ready tree. Seal verification
compares all rows, content references and the exact manifest outside SQL writes.

Ready index revisions bind a unit revision and content fingerprint. Multiple
knowledge publications retain the same accepted index without copying its tree.
Unbound tree retirement resumes across bounded calls, deleting deepest nodes
before entities and removing each content reference with its owning row. Real
SDK block retrieval over a borrowed connection and large CAS content is covered
with provider responses injected at the model boundary; no live-model claim is
made. Application integration remains outstanding.

Every candidate binds all selected processing units and document indexes,
prepares navigation before sealing, and verifies its exact ready tree before
publication. Navigation fingerprints contain fixed revision/publication and
input identities, schema/content policy and file digests; no execution model or
endpoint participates. Reads open only the retained tree. Missing bindings
raise `recovery_required`; `KnowledgeStore.repair_navigation` explicitly rebuilds
the same deterministic input and refuses a changed policy or manifest. Original
page, block, cell and character navigation policies share one pure assembler
with the existing application path. Tests cover empty navigation without write
side effects, retained page positions after external deletion, explicit repair,
and fixed historical short-source processing inputs.
Spec review additionally found that positive index ranges could still exceed
their retained source. Index acceptance and navigation preparation now validate
complete page/block coverage against the fixed original ordinals, rejecting
out-of-range or gapped intervals before they become public.

The repeatable synthetic calibration is `python -m scripts.verify_shared_storage
--nodes 5000 --directory . --output /tmp/shared-storage.json`. It creates and
removes a fresh disposable store; it does not inspect existing KB data. On the
same Python/SQLite runtime, the workspace filesystem is btrfs, while `/tmp` is
tmpfs. A real document tree with 5,000 navigation nodes built in 0.526 seconds.
Across 164 build transactions, held time p95/maximum was 2.432/5.893 ms.
Of 500 concurrent receipt updates, 41 began during the build; their full callback
latency p95/maximum was 59.894/192.909 ms. Overall receipt transaction wait
p95/maximum was 0.035/189.811 ms. The WAL reached 4,449,632 bytes.
Retirement used 160 calls, at most 87 tree rows per call, with call latency
p95/maximum 4.957/9.789 ms. No timeout, failure or remaining orphan tree occurred.
This calibrates protocol bounds on synthetic local data; representative
1k/5k/20k ledgers, ranking latency, end-to-end overhead and the other issue
performance gates still require validation.

### Recoverable Wiki projections and controlled external editing

Development schema 6 seals complete per-file synchronization plans before
publication. Final activation checks the captured projection generation and last
applied epoch under projection and KB coordination, then commits with the public
head. File replacement and its receipt are separate: an I/O failure leaves the
complete new publication readable and the worktree pending. Retries restore file
and directory durability before confirming a replacement or deletion; matching
bytes alone cannot establish that an interrupted directory sync succeeded.

The schema owns sync-plan headers, immutable items, the revision-to-plan binding,
the active projection pointer and last-applied file receipts. Each plan batch
has at most 42 items / 126 insert-reference rows and 256 KiB encoded metadata,
with a 64 KiB item limit. Activation is constant row work. Sync bounds each call
by item count and observed-file plus target I/O bytes. Old plans cannot roll back
the active target. Pending former active targets, unconfirmed new paths and
explicit discard baselines remain referenced across subsequent publications.
Recovery verifies the path, view, digest and generation before recognizing these
known intermediate states; arbitrary differences remain conflicts. Sealed items
cannot be updated or removed outside retirement. Managed deletions run before
new files, pruning only empty directories; skipped intermediate file/directory
changes also preserve the latest projection and unknown contents.

`application.wiki_worktree` provides the joint-development begin/accept/discard
use cases. Beginning an external edit durably pauses the entire projection and
retains its last confirmed per-file baseline, which can trail the master head.
Restart resumes the same pause. Master publication may continue while paused.
Accept uses a three-way comparison with that baseline and current master:
disjoint changes merge, competing changes keep the draft paused. Discard needs
explicit confirmation that external writes stopped, and unknown files remain
until individually selected. A sealed resumption plan activates atomically with
ending the pause. Its unfinished authorization survives newer publications.
No PID, editor exit or timeout silently resumes writes. Direct writes outside
this protocol still have a hash-to-replace race; the interface does not promise
atomic coordination with arbitrary external programs.

The 18 projection/edit tests and 107 related storage, lock, module-size and
application-boundary regressions pass. Full ruff and focused mypy pass. Standards
and Spec reviews exercised interrupted directory sync, SQL confirmation failure,
superseded targets, unconfirmed creation/deletion, discard continuation and
multi-generation file/directory changes; their verified findings were fixed.
These are development protocols, not full P4/P5 or issue acceptance: production
page writers, public resolvers, compiler, configuration and question adapters
still require joint integration, and P7 must retire unused plans and intermediate
roots while retaining all unfinished recovery chains.

### Prepared execution configuration and stage leases

The development `application.execution_configuration` captures a new store's
settings under its own short configuration lock and the existing external
lifecycle lease. It validates the explicit database identity both before and
after lease acquisition, even when management already owns an exclusive
lifecycle lease. This does not add a legacy-format reader or enable public v2
creation. Storage stages and conversation locks now carry that explicit identity,
so configured v2 test stores use the same lifecycle/reclaim ordering as empty
ones. No SQL snapshot or reclaim lease spans configuration-only or model waits.

The existing snapshot contents are shared through `config_snapshot`: effective
and raw settings, credential origins, in-memory credentials/environment, the SDK
model table and tokenizer asset identity. A parent execution keeps its activated
recipe while a fresh capture reads current settings. Capture refuses unfinished
settings journals without migrating or repairing files. Only explicit
`prepare_storage_config` recovers the settings pair and prepares retired capacity
templates. Both preparation and capture initialize the SDK's lazy environment
before resolving credentials, so a private endpoint cannot inherit an official
service's capacity merely because it is the process's first operation.

Settings merge patches retain the existing validation and crash-safe two-file
mutation protocol, under configuration coordination. They do not take the ingest
lock. The model-wait witness pauses the real ChatIndex SDK at its provider
boundary, concurrently changes settings, admits/reads a managed source and runs
explicit GC. These operations complete; the pending round still uses its original
settings and a later execution captures the new values. Fault tests additionally
cover settings-pair rollback, explicit preparation, fresh-process dotenv ordering,
unsupported schema refusal and both lifecycle identity paths. The user's prior
six-line configuration change remains separate and uncommitted.

Full mypy (472 modules) and ruff pass. The pre-review configuration/storage/lock
regressions passed 205 tests with one live test deselected; seven configuration
tests, including the two review regressions, pass after the corrections. No
live model service was called. The existing public `ExecutionContext`, question
and import adapters still need to select the joint v2 path; this entry point alone
does not satisfy the issue's actual ask/continue acceptance matrix.

### Guarded import publication and recoverable application results

Development schema 7 binds each import candidate to its exact intent, attempt,
dispatch, database generation, source revision, processing-unit revision and root
import. Its canonical revision manifest includes this immutable claim. Preparation
requires the claimed unit in the complete input inventory; final publication
rechecks the current source and unit target, dispatch and attempt state, group
cancellation and generation under the same short transaction as the expected head.
An unrelated retained input may remain historical without invalidating its source.

Import success, immutable publication receipt, head and synchronization-plan
activation now commit together. A failed receipt insert rolls all of them back,
leaving the sealed candidate available for retry. Separate pending-work completion
cannot mark an import successful. Database triggers reject success without its
receipt, later result reassignment and mutable attempt identities. Discovery work
and unsuccessful attempts retain their independent completion protocol.

The joint-development `application.publications.publish_import_result` returns a
retained candidate and `needs_review` reason for target or head conflicts. After
master commit, worktree I/O and SQLite failures report a published result with
pending synchronization. Entry and conflict recovery look up the attempt's exact
committed receipt, so response loss and two concurrently prepared candidates return
the original success; replay cannot rewind a newer head or activate its old plan.
Mismatched dispatch, view or generation cannot adopt that prior success.

The 15 import-publication witnesses cover changed source/unit, cancellation,
retried dispatch, transaction rollback/retry, worktree and coordination failures,
receipt immutability, lost response, concurrent reentry and complete claim checks.
Together with storage, projection/edit, lock, module-size and application-boundary
regressions, 125 tests pass (two live tests deselected). Full mypy (475 modules)
passes. Standards and Spec review findings have regression witnesses and fixes.
No live model service was called. This remains a P4 integration seam: the compiler,
all production writers/readers and execution gateway still need joint integration,
and issue #164 remains open pending its complete acceptance matrices.

### Fixed publication readers and retained page inputs

Development schema 8 records immutable per-page processing-unit revisions in the
revision manifest. Unchanged Markdown inherits its previous inputs even when a
source is processed again; explicitly changed pages can select a new basis. Those
retained inputs participate in navigation and reference closure. Manual external
edit acceptance preserves the input basis of existing pages. A publication can
therefore expose the new source while its older generated page still reads its
original body, native locations and images.

The explicit development `storage_scope` binds the shared application readers to
one publication. Page, image, catalog, backlink and query reads use that immutable
inventory and CAS, including after the editable worktree changes or becomes an
unrelated symlink. Source declarations resolve against the originating page's
inputs, with exact names preceding an unambiguous normalized alias. Retained
sources use version-scoped logical paths and selectors that cannot collide with
legal document names. Preparation rejects conflicting native source paths before
the head can change. Actual text, block and cell tools execute the capability's
bound path rather than reconstructing it from a displayed name.

Public navigation reads borrow the ready shared ConDB tree. They do not create a
private database or rebuild navigation for a different execution model. Missing
or mismatched navigation returns `recovery_required` with an explicit repair
action before any model request. The full SDK query holds its actual storage
stage; the surrounding application reading scope holds only the lifecycle lease.
An unrelated orphan can be collected between reads, and the ingest lock does not
block a fixed page read. Legacy page-context enumeration retains its existing
Markdown-only behavior without hashing every unrelated attachment.

The 17 new application witnesses include real SDK retrieval and real FunctionTool
execution against retained text/block sources. Provider responses are injected
only at the request boundary; no live model service was called. Standards and Spec
reviews reproduced and verified fixes for retained-source routing, ambiguous names,
native alias collisions and explicit navigation recovery. The related storage,
reading, query, application-boundary and module-size regressions pass 183 tests
(24 live tests deselected). Full mypy (477 modules), ruff, format and diff checks
pass. This is still joint-development integration: application writers, compiler,
execution gateway, public ask/continue and the remaining P6-P8 gates are unfinished.
Issue #164 remains open.
