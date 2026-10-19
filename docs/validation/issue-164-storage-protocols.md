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

### Application page saves and deletions

An explicitly bound development `storage_scope` now routes the existing shared
`save_page` and `delete_page` use cases through immutable revision preparation and
the final publication protocol. The scope itself supplies a fixed internal
publication baseline; supplied body versions and publication confirmations are
also checked. A racing publication at final commit returns a conflict and retains
the prepared candidate. The response keeps the caller's draft and can return the
latest accepted page without changing an already bound reader.

Saving preserves the original metadata block and treats the entire incoming text
as body, including a legitimate opening delimiter block. Link normalization is
shared with the existing file writer. Deletion prepares a complete private
Markdown baseline and reuses the existing index-entry and backlink cleanup;
unrelated pages retain their pre-existing links. Existing page input revisions
survive manual edits, even when the publication's active source is newer.

`KnowledgeStore.prepare_changes` reuses the fixed baseline's immutable files,
active index/unit identities and per-page inputs. It validates exact database
identity, live Blob metadata, complete manifests and references, while reading
new content rather than rescanning unchanged assets. Review found an additional
asset reread in synchronization-plan preparation; its replacement verifies the
sealed complete manifest and exact slot/digest/size/live reference closure. An
actual file-open witness now rejects any unchanged CAS image read during patch
preparation. Later worktree synchronization still validates the files it touches.

The master commit establishes success. Page results carry separate projection
states, so failed synchronization returns saved/deleted with pending files, and
a persistent external-edit pause preserves the draft while a new master version
is published. Eleven new application tests cover these paths, stale baselines,
final save/delete races, retained source inputs and asset reuse. Both review axes
independently verified the corrected behavior. The related storage, page, lint,
projection, import, module-size and application-boundary regressions pass 221
tests; full mypy (478 modules), ruff, format and diff checks pass. No live model
service was called. Saved answers, other tool writers, repair/removal, compiler,
execution and public question adapters still require integration before P8.

### Durable execution at the actual model gateway

An explicit development `ExecutionContext(storage_identity=...)` now captures the
prepared configuration once and starts a main-store task, execution and model
budget before activating the real CompletionExecutor and Agents SDK gateway.
Its outer protection is the lifecycle lease. Short storage stages persist each
request intent and send marker before transport, so a pure HTTP wait does not
hold the reclaim lock or the configuration lock. The normal creation path still
uses the existing format; this is not the P8 activation switch.

Development schema 9 tracks whether each request still charges its admission
reservation. A terminal request known not to have sent releases that reservation
once; unknown transport remains conservative, and later actual send evidence
charges the original budget. Actual-send counters are separate from reservations.
Request callbacks merge per-field versions and retain cancellation regardless of
arrival order. Evidence takes precedence over version order: actual transport
beats an explicit SDK application-cache hit, which beats an unknown result.
Application reuse records zero usage without inventing a network send; call-level
SDK usage cannot replace transport usage or manufacture a missing provider bill.
Cache-packet replay cannot erase real usage or fill its unknown breakdowns.

Consumed HTTP error bodies and successful SDK fallback responses are independently
retained before business continuation. SQL or CAS acceptance failures create a
durable response or usage-only repair packet and stop automatic provider retries.
Repair does not invent a response for a usage-only packet. Indexed request paging
and incremental usage counters avoid scanning the ledger in progress callbacks.

After the caller has actually settled its SDK and worker work, cleanup revokes new
send capabilities and terminates abandoned known-unsent intents. The original
controller can still accept late receipts; another execution cannot use its
request identity. Stage-observation write failure preserves the primary model
error and does not leave the settled execution running. Unconfirmed stage rows
remain explicit gaps for later recovery, rather than supplying invented timing.
This does not yet implement fresh-process compiler TaskJournal recovery or the
complete non-model stage and work-item coverage required by P6.

Seventeen new gateway tests exercise the real SDK against loopback HTTP errors,
provider-boundary cache/unknown results, transport cancellation, concurrent GC and
settings changes, SQL faults and repair, duplicate initialization, foreign
callbacks, cleanup races and out-of-order evidence. Both review axes verified the
fixes, including all six arrival orders of cache, actual transport and cache
replay in independent SQLite witnesses. The related execution, usage, storage,
configuration, projection and size regressions pass 115 tests (35 live tests
deselected). Full mypy (480 modules), ruff, format and diff checks pass. No live
model service was called. Public ask/continue, full compiler integration, remaining
writers and P6-P8 acceptance still remain; issue #164 stays open.

### Public questions with fixed evidence and streamed receipts

The existing shared `ask_question` use case now supports an explicitly bound
development `storage_scope`. It captures configuration once, builds the real
Agents SDK reader against the selected immutable publication, and holds no
whole-question ingest or reclaim lease. A gated actual HTTP wait allows an
unrelated orphan to be collected and settings and knowledge to change while the
answer still reads its original evidence. Saving against a changed head retains
the answer candidate and reports the conflict without another model send.

Consumed SSE bytes now pass unchanged through the actual HTTPX/LiteLLM streaming
path and are independently retained on completion, close or SDK cleanup. Partial
provider usage merges known nested details without turning missing breakdowns
into zero. Failed response acceptance leaves a repair packet and stops retries.
Results and progress use the same validated UsageReceipt schema and indexed
counters; an observational read failure cannot undo a saved answer.

Question logs and optional uniquely named saved answers publish together before
worktree synchronization. Save preparation runs in a worker, cancellation waits
for that worker, and final acceptance checks the stop signal. Cancellation before
commit keeps the previous publication; cancellation after commit preserves the
saved result. Projection pauses and failures remain separate from master success,
as do execution cleanup and usage observation failures.

Answer references carry their exact database identity, publication and content
digest. Old reference reads capture only explicitly referenced image bytes from
that publication. The actual Qt reader displays the old pixels after a newer
publication, rejects unbound file images, and still renders generated formula
images. Source-read attribution distinguishes processing revisions even when
they share an original. Saved answers retain the complete source basis needed
after their original citing page is deleted.

Fifteen new application/Qt cases cover these behaviors using real temporary
SQLite/CAS and SDK requests to a controlled loopback provider. The related
question, execution, reading, editing, projection, citation, adapter and size
regressions pass 138 tests (59 live tests deselected). Full mypy (483 modules),
ruff, format and diff checks pass. No live model service was called. Public
continue, full compiler integration, other writers and P6-P8 acceptance remain
unfinished; the default creation path is unchanged and issue #164 remains open.

### Continuation, accepted answers and user-only recall

The shared `continue_conversation` use case now has an explicit development
main-store path. Its parent holds execution and lifecycle protection while the
actual worker acquires the real session lock. Synchronous ChatIndex work and
async Agents requests run on that worker, without borrowing a connection or a
session lock from another thread. Cancellation signals the executor and waits
for the worker and its SDK cleanup; a committed result is latched before the
worker event loop shuts down.

Development schema 10 records immutable user submissions, execution attempts,
accepted answer candidates and committed turn metadata. Complete original user
and assistant messages remain the authoritative CTree-compatible turn payload.
An accepted full answer and its citations are retained in their own transaction
before the atomic turn/result commit. A failed turn transaction can therefore
resume the same submission from its candidate without another model send.
Duplicate completed submissions return the original answer and references, and
reject changed user text or submission order. Current-run receipt binding keeps
an earlier success from hiding a new validation failure.

Both answer acceptance and final turn acceptance check the actual execution's
generation and active state inside SQL. Persisted stop wins even before the
in-memory signal changes; restored stores reject original execution tokens.
Completion cannot be assigned through the failure-finalization API: schema
constraints require the exact submission's turn and result, and completed
receipts are immutable. Raw provider receipts and usage survive failed or
cancelled turn commits. Known terminal `not_sent` attempts can retry; a paid,
unknown or still in-flight attempt without a safely accepted complete answer is
blocked for recovery rather than automatically resent. Full SDK/tool-state
replay for that latter case is still pending.

Recent-user capacity allocation is shared with the existing policy. Preparation
retains the 8,000-token threshold and six-recent-turn rule, checks recall capacity
and older candidates before indexing, and never inserts earlier assistant text
as user intent. Real ChatIndex catch-up commits each accepted prefix and builds
one final ConDB projection. Recall opens that shared ready tree and validates
selected exchange identities before rereading bounded original user excerpts.
Budget-limited computation reports its real computed/retrievable prefix and
pending turns; successful recall does not conceal incomplete indexing.

Twenty new application and crash-boundary cases cover these behaviors, including
actual model waits concurrent with GC, settings updates and another publication,
queued cancellation/deletion, SQL failures, lost acknowledgements and restore.
The related question, session, memory, storage, reading, adapter and size
regressions pass 172 tests (29 live tests deselected). Both review axes verified
the corrected boundaries; full mypy (487 modules), ruff, format and diff checks
pass. All provider responses were controlled locally; no live model service was
called. Session management/export, model write tools, remaining compiler and
writer integrations and the full P6-P8 gates still remain. No default activation
or issue closure has occurred.

### Paged conversation history and guarded deletion

The shared history, list, prefix/latest resolution and deletion use cases now
accept the explicit development `storage_scope`. Schema 11 gives conversations
bounded catalog ordering and an activity version distinct from the accepted
original-turn revision. Starting a pending submission invalidates an earlier
delete confirmation even before another answer is committed. Deletion takes the
real session lock, checks the exact KB generation, view and confirmation version,
then commits a tombstone. Original turns and core execution/usage records remain
retained; reusing the deleted identity cannot revive the conversation.

History reads only the selected ordinal range and its answer metadata. Pending
original questions have their own bounded cursor in stable submission order.
Catalog pages read metadata without loading full histories. Reads take no session
or ingest lock and finish by checking the captured activity version, reporting a
conflict if the history changed during capture. An actual gated HTTP request
demonstrates that the preceding completed answer and current pending question
remain readable while a model waits.

The versioned display trace refers to its immutable final assistant original
instead of storing that full body again. Rendering reconstructs the original
trace, preserving tool calls and preceding narration. This is a small display
record encoding and does not change CTree snapshots or the raw conversation
protocol. Provider response receipts retain their independently required bytes.

Ten new management cases cover pagination of 70 original turns without opening
unselected payloads, bounded catalog cursors, ordered pending questions,
confirmation conflicts, model-wait reads and lossless trace references. Cursor
integers reject values outside the nonnegative SQLite int64 range. Restored
history and completed-submission replay validate the original KB, exact
publication, view, path and digest before rebinding only the displayed reference
to the current runtime identity. The original provenance and CAS records remain
unchanged; a stale reference passed directly still fails the generation gate.
These are controlled local SDK/SQLite/CAS witnesses, with no live model service
called. Both review axes verified the fixes. The related storage, session,
application, adapter, citation and module-size regressions pass 203 tests
(32 live tests deselected), including six native Qt cases. Full mypy (490
modules), ruff, format and diff checks pass.
Session export, model write tools, the full compiler and writer integration and
P6-P8 acceptance remain outstanding. This batch does not activate the new format
by default or satisfy the overall issue closure gate.

### Independent model note publications

The explicit main-store continuation path now installs a revocable writer for
the actual `write_file` tool's `wiki/explorations/` notes. Each note publishes
independently through the same immutable revision and projection protocol. The
writer advances its private expected publication after success while evidence
readers retain the question's original publication. A concurrent publication
keeps the candidate for review and latches the conflict, so the SDK cannot hide
it behind later final text. Previously accepted resources and their exact last
publication remain reported even if a later write conflicts or the turn fails.

The final publication transaction and original-turn acceptance share execution
generation/active-state validation. A durable stop therefore wins even before
the in-memory stop signal changes. Actual writer settlement revokes copied tool
contexts before accepting the final turn. No writer, ingest lock or reclaim
lease spans the following model wait. A failed turn can adopt its independently
accepted complete answer without sending again or repeating accepted notes.

An exact committed-publication lookup resolves a lost acknowledgement using the
original revision, manifest, navigation, projection plan and receipt identities.
It neither reruns the model nor treats another publication as success. Storage
confirmation failures latch recovery instead of permitting a false completed
turn. Output-directory artifacts, full SDK/tool replay and the other writer
integrations remain pending; these note capabilities still require an explicit
development scope and do not complete issue #164.

Nine new loopback/SQLite/CAS cases cover these boundaries. Both review axes
verified the batch. Related storage, question/session, adapter, citation, legacy
tool, mutation and module-size regressions pass 252 tests (33 live tests
deselected), including six native Qt cases. Full mypy (491 modules), ruff,
format and diff checks pass. No live model service was called.

### Independent generated artifact bytes and projections

The same development model writer now accepts `output/` text artifacts into
the main store and CAS before replacing their editable copies. Schema 12 adds
immutable artifact revisions tied to the actual execution and evidence
publication, a single expected output epoch, and per-file current/applied
projection state. These records serve the actual `write_file` tool and shared
`read_artifact` use case; they are not a general file repository or journal
framework. Concurrent output writers retain a conflict candidate instead of
overwriting a newer accepted output. The writer advances only its private output
epoch after its own acceptance.

Raw artifact reads use accepted immutable bytes, independently of missing,
outdated or externally edited working files. An explicit revision selects the
exact saved or conflicting original. Projection compares the last confirmed
applied digest, preserves detected external differences, and rejects an obsolete
revision after another head has advanced. An I/O or confirmation failure returns
pending synchronization after master acceptance. Only an applied HTML copy
returns the tool success phrase that emits a preview card. This retains the
same best-effort limits against arbitrary external filesystem races as other
digest-before-replace working-copy checks.
Before replacement, synchronization durably activates the exact possible target
and its validated physical baseline. These remain recognizable across newer
heads until confirmation, including a crash before replacement or after a prior
replacement whose SQL receipt failed. Never-activated accepted content cannot
authorize replacing different external bytes. File and full parent-chain
durability is checked on every retry before clearing the pending activation.

Each artifact operation has an exact request identity for lost-reply recovery,
including bytes, expected epoch, execution and evidence publication. Immutable
receipt replay does not advance the epoch again or accept changed arguments.
The final transaction checks the execution's durable stop and runtime generation;
restored stores reject old execution callbacks while retaining readable original
artifact bytes. Turn failure leaves accepted artifacts intact, and accepting the
retained complete answer does not repeat those writes or model sends.

Eleven new local SDK/storage cases cover these boundaries. Artifact directory
browsing, archives/export, all generator entry points, output-version retirement,
and full restored working-copy synchronization still need integration. These
capabilities remain development-only and do not satisfy the complete issue gate.
Both review axes verified the durability and activation recovery fixes. Related
storage, application, conversation, citation, legacy tool, mutation and size
regressions pass 267 tests (33 live tests deselected), including six native Qt
cases. Full mypy (493 modules), ruff, format and diff checks pass. No live model
service was called.

### Fixed artifact catalog selections and complete exports

The development artifact catalog now reads bounded master metadata instead of
walking editable output directories. Each page exposes its output epoch and
knowledge publication, carries an advancing cursor even across hidden or newer
names, and keeps Skill/deck packages grouped. Directory cursors skip their
descendants by an indexed range; a hidden final file uses an exact file cursor
so similarly named visible siblings remain reachable. File/directory name
collisions retain a conflict candidate instead of accepting an impossible
working-copy tree.

Shared artifact reads, selected file enumeration, archives and collision-free
exports accept the selected epoch explicitly. Default exports capture a fresh
epoch once. Output bytes come from that accepted history; reports and exploration
notes come from the exact selected knowledge publication. Working-copy edits,
missing copies, unknown files and filesystem symlinks cannot alter the selected
contents. Export targets reject other main stores even before those stores have
configuration or a materialized wiki.

Explicit complete archives page their selected names and hold the shared reclaim
lease throughout actual reading, with no ingest lease or SQL transaction across
ZIP I/O. Verified CAS copying uses one-MiB chunks, checking length and SHA before
reporting success; failed file exports remove their partial target. This allows
complete binary attachments without a whole-body export allocation. A 70-file
archive witness updates a later selected file and adds another while copying:
the archive retains exactly the original version and GC reports busy until the
actual read finishes.

Ten new local storage cases cover these boundaries. Both review axes verified
the fixes. Related regressions pass 277 tests (33 live tests deselected),
including six native Qt cases; mypy (494 modules), ruff, format and diff checks
pass. No live model service was called. The public factory and adapters remain
on the prior format until joint integration and P6-P8 acceptance are complete.
Full generator writers, compiler integration and maintained output retirement
remain outstanding; this batch does not close issue #164.

### Graph reads and independently accepted graph output

The actual graph reader now resolves selected main-store Markdown pages rather
than scanning the mutable wiki. Manifest queries page names without opening
binary assets; graph construction retains the existing node/link algorithm.
A fixed-publication witness replaces the working wiki with a symlink to unrelated
pages and advances the knowledge head: both selected graph nodes and links still
come from the original publication.

The development graph generation path creates a distinct `generate_graph` task
and execution with a zero model-call budget. It records actual versioned collect,
render, artifact acceptance and projection stages. Model settings and credentials
are unnecessary for this deterministic operation. The lifecycle remains held;
neither an ingest lease nor a reclaim lease spans pure HTML rendering. Accepted
HTML uses the same output-epoch and exact-request acknowledgement protocol as
the real model writer, with that protocol shared in one application helper.

Graph results distinguish retained revisions, applied copies and confirmation
gaps. A filesystem or stage/execution receipt failure cannot erase an accepted
HTML result or a retained conflict candidate. Conflict remains the primary
classification while a separate confirmation flag reports failed cleanup.
Stopping before artifact acceptance leaves no output head; stopping after
acceptance retains the original and records a cancelled execution. Stage
admission now shares the central active-generation check inside its existing
transaction, atomically mapping durable stop to cancellation.

Ten new local storage cases cover these boundaries, including the conflict plus
receipt-failure combination. Both review axes verified the fixes. Related
regressions pass 290 tests (33 live tests deselected), including six native Qt
cases; mypy (495 modules), ruff, format and diff checks pass. No live model service
was called. Compiler and remaining generator integration, full task observability
and maintenance, adapter activation and the overall issue closure gate remain
outstanding.

### Full offline workspace regression after graph integration

At implementation commit `6c243198`, the current workspace passes **3,620 tests**,
with 14 platform/optional skips and 745 live tests deselected (516.80 seconds).
The offline harness rejects non-loopback traffic and real-model fixtures.
Full-repository ruff and format checks pass (987 formatted files), as does mypy
for all 495 source modules. This run includes the original uncommitted compiler
recovery/config-capture changes. Their edit bodies are preserved and none of
their seven tracked files is included in the three artifact/graph implementation
commits. This is workspace regression evidence, not a clean-release or live-model
acceptance claim.

The next compiler integration must preserve the complete recovery contract.
`compile_short_doc` requires a controlled writable compiler view, while a bound
main-store publication is read-only. `document_checkpoints` currently returns
no checkpoints for a new-store execution because it has no legacy `model_task`.
Passing only a temporary Markdown copy would therefore omit accepted plans,
raw response recovery, repair/conflict allowances and immutable page bases.
Those responsibilities and exact work/request identities must use the main
store before enabling the actual compiler path. Managed admission/indexing,
remaining generators, full task observations, maintenance/restore roots and the
three entrypoint adapters still require joint integration and final acceptance.
Issue 164 remains open.

### Actual retained-unit compiler path (development scope)

The public `recompile_document` use case now accepts an explicit main-store
scope and processing unit. Its worker owns a complete private Markdown baseline,
loads retained normalization and native inputs, and runs the existing short or
long compiler. Long compilation consumes the fixed publication's accepted
shared document tree; it neither copies nor opens an old private index database.
An existing page's old original uses a revision-qualified alias when the current
target advances. Publication retains unchanged page bases and the exact pending
unit claim, including its dispatch, attempt, source target and generation checks.

Main-store executions now require an explicitly bound compiler checkpoint owner.
Accepted results, page bases, raw deliveries and repair/conflict flags remain
independent of publication. The real gateway associates compile invocations with
actual request stages; a received summary or plan can be recovered from that
exact provider receipt if compiler acceptance/delivery was interrupted. Plans
are revalidated before reuse. Complete checkpoint metadata uses CAS roots while
its SQL row stays bounded. Publication success survives a later projection
coordination failure and reports synchronization work still outstanding.

New application cases use the actual compiler and SDK against a controlled local
HTTP provider. They cover removed external originals, short and multi-window long
compilation, page creation with complete published Markdown visibility, exclusion
of live drafts, old/new source separation during shared-page updates, one response
repair, raw summary/plan delivery recovery, accepted-step reuse after publication
failure and projection failure after a successful publication. No real model
service was called. The development schema is now version 13; no migration or
compatibility path was introduced.

This is a compiler integration batch, not the full migration or activation gate.
Related offline regression passes 435 tests (75 live cases deselected); full
repository lint/format and mypy for 500 modules also pass. The run includes the
pre-existing uncommitted compiler/config changes, whose edit bodies remain
outside this batch's commit.

Managed admission and index construction, complete identity/partition/recovery
fault coverage, necessary historical figure attachments, full task observation
and maintenance/restore workflows, remaining generators and all three adapters
still require joint acceptance. The public factory stays unchanged and issue
164 remains open.

The two-axis review of `8ba53972` found two protocol/reporting defects and
three request-recovery defects. Follow-up changes move record JSON work outside
write transactions and compare the prepared prior value at commit; recognize
the Wiki projection's `synced` result; associate normal update and partition
sends with concrete invocations; recover a newer paid repair before an older
invalid capture; and examine the invocation's received/uncertain attempts rather
than treating its last known-unsent retry as proof that nothing was sent. A late
older execution can retain a response without replacing a newer delivery pointer.
New failing-first application and storage-window cases cover these findings.

### Compiler image inputs and late-response races

A raw delivery is now retained independently even if a newer invocation wins
the optional latest-response pointer race. The final transaction still checks
the frozen request and provenance. The competing invocation regression fails
before this fix and passes after it; both review axes independently confirmed
the original recovery findings and this adjacent race are resolved.

Compiler inputs now map a native source's image filename to its exact retained
processing revision. Old and current sources may name the same image while
preserving different bytes and occurrence identities. The private workspace
selects existing inline and historical-gallery attachments from the complete
fixed Markdown baseline, including galleries without a remaining factual input
basis. It copies referenced native images rather than every image in a processing
manifest. Application witnesses update the same shared page with red/blue source
versions after deleting the external originals; both images remain readable,
the prior publication is unchanged, and unrelated publication/processing images
and private index databases are not copied.

Related compiler, usage, figure and retained-source regressions pass 41 tests
(11 live cases deselected); mypy passes all 500 source modules. The remaining
integration and activation gates above still apply. No live model was called.

The next spec review found two attachment gaps: the shared image inventory
omitted SVG/TIFF/ICO, and ordinary inline image paths could still change bytes
when an input advanced under the same filename. Failing-first public compilation
cases now cover both. Supported retained formats are included; non-source pages
in the private fixed baseline relocate existing inline references to verified
content-addressed assets, preserving URI query/fragment and original factual
bases. Only a later substantive compiler edit adds the target unit to that page's
basis. Source originals remain unchanged. This disposable normalization uses the
branded streaming helper without file or directory fsync. Related regressions
pass 73 tests; full mypy still passes 500 source modules.

Follow-up review added public cases for HTML attribute quoting and manual pages
with an explicitly empty factual basis. Relocated HTML URLs now escape decoded
query/fragment values; unchanged normalized pages explicitly retain even an
empty basis rather than inheriting publication defaults. The ten image
application cases pass. Both review axes confirmed the adjacent fixes in the
workspace; no real-model traffic or original user edits were involved.

### Actual shared document-index construction

Development-scope recompilation can now build a missing document index directly
with the existing PageIndex pipeline and accept its complete document into the
shared tree store before publication. Retained Markdown uses the pinned parser;
frozen content blocks reuse the existing qualification adapter without packaging
or copying their assets again. Physical page and slide body/notes inputs keep
their original text, ordinals, roles and coordinates. No private index database
or legacy index-stage directory is created.

Exact model requests bind their own invocation in the calling thread/task, so
concurrent SDK stages with the same display stage cannot borrow another request's
identity. Raw responses and accepted per-node/stage results use the main recovery
store. The SDK's mutable dispatch count remains a small consecutive guarded
record; individual dispatches and accepted work stay independently durable.
Missing adapter delivery recovers the exact gateway receipt, including its real
finish status, before considering a new send. A ready index is reused after a
later publication failure. Scheduling concurrency is excluded from its content
identity; selected model, generation, endpoint, policy and retained unit remain
bound.

Six real SDK/loopback application cases cover Markdown and native block/page/slide
inputs, publication failure, raw response confirmation failure and partial node
acceptance. Related offline regressions pass 88 tests (8 live cases deselected),
and full mypy passes 502 source modules. Managed public admission, the rest of
the compiler fault matrix, maintenance, task observations, other generators and
P8 activation remain required; this does not close issue 164.

### Public managed document admission (development scope)

`import_document` now routes an explicit main-store scope through original and
sidecar admission, retained native processing and the actual shared compiler.
The operation captures settings once, uses complete fixed Markdown for capacity
assessment, and keeps conversion/private preparation outside the old-format
ingest lease. The existing converter accepts only an owned private target when
that lease is omitted, with an explicit document name and no registry lookup.
Existing Markdown/text conversion, PDF page extraction, capacity selection and
block policy remain shared. Long text freezes its native blocks without creating
an unused input package. Accepted originals retain their identities even when
normalization confirmation fails.

Public application cases cover Markdown with a sidecar, zero-send repeated
admission, external-file deletion followed by real recompilation, full and
segmented PDF import, failed normalization and long text with multiple SDK
structure fragments. The eleven import/index application cases pass; 139 related
offline regressions pass separately, and the long-text case passes after correcting
the controlled provider's continuation reply. Full mypy passes 504 source modules;
lint/format checks pass. These runs include the unchanged original workspace edits.

Workbook units and discovery, retain-without-knowledge handling, complete explicit
processing-policy/recovery paths, task observations and maintenance, remaining
writers and all three adapters still need joint acceptance. The normal factory
continues to use the prior format until P8. No live model was called.

Review found two public admission defects. Failing-first cases now inject failed
processing/publication reads after the actual publication commit, and route an
explicit credential bundle between two controlled HTTP providers. Import carries
the committed knowledge revision directly in its compiler outcome and reads its
processing decision before compilation. Explicit credentials enter the frozen
recipe before executor creation; a mismatched already captured recipe is rejected
before sending. All 35 targeted admission, indexing, configuration and compiler
cases pass, and full mypy still passes 504 source modules.

### Shared backup and restore: configuration and paused edits

The shared maintenance use cases now return a committed backup/restore receipt
without re-reading files after publishing the destination. Versioned backup
manifests describe local KB configuration separately from inherited global
settings. Configuration uses the same short settings coordination; original
permissions, bytes and presence are preserved. Restore validates the manifest,
file digests and reference closure before creating its destination, under the
destination lifecycle lease.

Paused worktree changes are captured under projection coordination alongside the
online snapshot, without claiming the editor stopped or accepting any change.
Observed concurrent external changes abort the backup. Restore reconstructs the
actual applied files from retained Blobs, then overlays changed/new files and
deletions while preserving the persistent pause. Subsequent explicit acceptance
merges against the restored master. This also reconstructs applied copies for
ordinary unpaused views. Arbitrary uncoordinated editor writes still cannot be
promised an atomic filesystem snapshot.

Four public maintenance cases and ten related storage/editor regressions pass;
full mypy passes 508 source modules. External pending receipts and interrupted
runtime recovery are still outside this batch's coverage, explicitly reported in
the backup, and remain required before P7/P8 acceptance.

### Backup pending receipts and restore runtime recovery

Pending packet writes, explicit repair and backup now share the existing short
KB write coordination. The online snapshot and packet capture use that
coordination together; known packets are matched to snapshot request provenance,
while malformed/unknown packets are copied exactly and report partial coverage.
Restore accepts known packets against the original generation before establishing
the new runtime generation. Unresolved packets remain present and explicitly
incomplete. No received response is re-sent for this recovery.

Restored active executions, stages, pending attempts and requests become
interrupted without fabricated end times. Request counters are updated through
the existing receipt merge; original request provenance and spent budgets remain.
Import groups are rebound for new dispatches, while prior attempts retain their
old generation. A public compiler case resumes accepted model results after
restoring into another directory, creates a new execution with the original
budget, publishes successfully and makes no additional provider request.

Two review findings were reproduced first and repaired: coverage is validated
against the manifest contents, and restored working-copy bytes are verified after
the actual atomic copy before publishing the new directory. Ten public backup
application cases now cover configuration corruption, paused edits, late payload
changes, known/unknown packets, running tasks and restored compilation. Related
maintenance, usage, model, pending, session, artifact, compiler and editor
regressions pass 89 tests. Remaining P6/P7 product queries, manual retention and
the full P8 matrix are still required.

### Conservative recovery ownership during manual collection

Shared collection/preview use cases now inspect pending ownership under existing
coordination. Unknown, malformed or bounded-scan-overflow packets protect every
candidate and return a reason with zero deletions; exact known packets carry
their own response bytes. Three public GC cases cover malformed/unknown requests,
retained orphan bytes and GC busy throughout actual backup Blob copying. Pending
packet parsing is shared with explicit repair and backup.

Review also found that incrementing a legal maximum provider receipt version
could make restore fail. A real running-question backup reproduces this boundary.
Private restoration now changes request runtime state and counters independently
of provider field versions, without inventing a later receipt or completion time.
The accepted provider versions, spent calls and historical identity remain intact.
Seventy related GC, backup, model, usage, compiler and tree cases pass.

The restore review additionally reproduced a missing endpoint guard: an import
using explicit provider B could resume B's budget/checkpoints after a restored
configuration selected A. The public execution identity now includes a one-way
endpoint fingerprint, retaining no credentials or endpoint text. A dual-provider
public backup/recompile case now rejects the changed recipe before creating a new
execution or sending, while the unchanged endpoint continues with zero resend.
Thirty-eight relevant offline cases pass (48 live cases deselected).

### Bounded task queries and actual import stages

Shared task list/detail use cases now return versioned validated payloads from
the main store. Executions, actual stage attempts and request metadata have
independent bounded cursors; a selected execution must belong to its task. JSON
export uses the same page schema, keeps continuation cursors/coverage, and never
reads prompts or response bodies. Request callbacks update task counters in the
same short transaction as existing counters. Lists read those bounded summaries;
details distinguish current execution usage from cumulative task usage and expose
the original budget across retries.

Import and compilation now persist actual admission, processing, materialization,
generation, validation, retention, publication, Wiki synchronization and cleanup
boundaries. Model requests bind their actual generation parent. Observation
failure cannot skip required SDK cleanup, and cleanup is allowed while stopping;
published outcomes and admitted source identities retain their existing latches.
The public query case follows real SDK traffic, pagination, exported JSON,
zero-send repeated import and cross-task identity rejection. Coverage explicitly
marks remaining non-model/resource/price observations incomplete rather than
inventing measurements. Publication-result links and the three presentation
adapters remain the next integration work.

Seventy-eight related regressions, including file-size/boundary checks, pass;
full mypy passes 515 source modules and lint/format checks pass. Issue 164 remains
open and P8 remains the sole normal-format activation point.

### Committed publication facts and task-query boundary repairs

The development schema is now 14: a publication receipt optionally records its
actual execution in the same guarded publication transaction, with an indexed
query. No old-store migration or normal-format activation is introduced. Task
details page those committed publications separately, including exact source/unit
revision identities and the corresponding projection state. A public fault case
fails publication-stage confirmation after commit: import remains added, the
task's business result remains completed, and its pending Wiki publication is
still visible. A failed observation does not turn committed knowledge into a
failed business result.

Review reproduced two adjacent defects. A progress subscription failing while
entering cleanup could still skip the controller; the required cleanup body now
runs even through that entrance failure before surfacing the diagnostic. Task
export also now shares generated-artifact export's every-KB marker check, so an
export cannot overwrite another KB database or worktree. Actual SDK/controller
and double-KB regressions went from failing to passing. The four task application
cases and related publication, database, backup, compiler and size checks pass
73 tests; 34 related export/model cases pass separately. Full mypy passes 516
source modules. Three-adapter presentation and remaining P6/P7 acceptance remain.

Task-publication review then reproduced stale projection status after explicit
discard activated a resumption plan for the same head, and a superseded plan
retaining old conflict counts. Details now select the current matching resumption
plan when present and classify supersession before former conflicts. Two public
import/editor application regressions prove pending-to-synced resumption and
historical supersession after a later successful import. All 24 related task,
editor and projection regressions pass.

### Shared task pages in three adapters

CLI `tasks list/show/export`, authenticated HTTP `/api/v1/tasks` detail/export,
and a native Qt task browser now consume the same shared application schema and
independent cursors. Main-store reads validate the exact format without depending
on an already-published Wiki or activating the new-format creation factory. A
worker execution resolves to its actual durable task instead of reinterpreting
the application cache's task ID. Details include selected-execution metadata even
when its execution-list page differs. Exports remain bounded and preserve the
statistics time, schema, coverage and continuation cursors.

Qt uses cancellable storage reads on LocalIO worker threads, binds the database
generation once, and discards obsolete responses. Pausing refresh affects only
subscription. An actual exclusive reclamation lease blocks a detail read while
Qt continues delivering events; closing cancels that wait before the lease is
released. CLI, HTTP and native Qt detail/export payloads agree after excluding
the observation timestamp, including a separately advanced stage cursor. API
queries retain the existing registered-alias and authentication boundaries.

Fourteen focused task/entrypoint/size cases and 195 existing CLI/API/desktop/
boundary regressions pass (five live cases deselected). Full mypy passes 519
modules; the subsequently extended query DTO and adapters also pass targeted
mypy. This is task presentation integration, not completion of P6/P7/P8: input
drilldown, remaining real stage producers, archival/statistical maintenance and
normal-format activation remain unfinished. Issue 164 remains open.

Both review axes reproduced adapter-only gaps: old directory discovery rejected
an unpublished main store without configuration/projection, a paged-out selected
execution was mislabeled latest, and export could silently follow a newly added
execution after refresh was paused. Task routes now resolve only the existing
KB alias and let the shared main-store gate validate authority; CLI explicit
paths/current ancestors likewise reach that gate. Qt independently preserves
the selected execution, anchors export to the displayed execution, and leaves
an unlinked queued task unselected with an explicit message. Real SQLite/Qt
regressions cover each boundary; all 27 related task, UI and size cases pass,
as does targeted mypy.

Follow-up review also checked nearest-KB discovery and an export while the next
page is still waiting on reclamation. CLI now stops at the nearest metadata root,
so it rejects a nested old-format KB instead of silently reading its v2 parent.
Qt exports the last displayed page's task, execution and cursors, independently
of an in-flight selection. Twelve task/query regressions pass; independent
standards review passes all four focused SQLite/Qt boundary reproductions.

### Independent workbook units and managed source retries

Development schema 15 retains complete sparse worksheet inventories as immutable
CAS-backed records, independently of processing/publication. Processing records
distinguish actual content, empty input and unreadable/failed extraction; only
content can enter compilation/public knowledge. The existing pinned XLS/XLSX
readers and worksheet reconciliation/normalization algorithms are reused. Every
sheet target is retained before model work; a damaged sheet does not roll back
other successful sheets. Whole-inventory failure preserves the last complete
identity basis rather than manufacturing new units after a repaired upload.

The shared source retry now materializes managed originals/sidecars, checks exact
source/unit targets, and resumes the original execution's task and paid budget.
It never needs the external original. Per-source incremental counters distinguish
this execution from source history without scanning request records. Successful
units are skipped without sending. XLS inventory and target identities survive a
full online backup/new-generation restore, with zero-send resumed import.

A real reordered/revised workbook exposed a pre-existing compiler-selection
mistake: historical page dependencies were being promoted into active inputs.
Materialization, reuse and publication now use the explicit active revision set;
historical dependencies retain separate native aliases and exact old bytes.
New worksheet work binds each accepted publication before continuing, and keeps
accepted unit outcomes if a later binding fails.

Both review axes reproduced partial-result loss during execution cleanup and
successful retry-result loss during final usage reads. Import and source retry
now share final usage confirmation and preserve all returned unit outcomes;
unknown confirmations are unfinished work, not replacement business failures.
Stopping a retry records cancelled rather than failed. Nine workbook application
cases cover malformed/empty sheets, XLS/XLSX, independent publications, managed
retry, reordered cells/history, bad-inventory continuity, backup/restore, cleanup,
usage-confirmation and cancellation faults against real SQLite and loopback SDK
requests. Fifty related compiler/reading/task cases and 46 maintenance/usage/
entrypoint/boundary cases pass; the earlier 74 legacy workbook/database/compiler/
backup regressions also pass. Full mypy passes 522 modules, with lint and format
checks passing. P8 remains disabled and the full issue remains open; retained
non-text inputs, remaining compiler/writer paths and P6/P7/P8 acceptance continue.

### Managed non-text retention and fixed native source reads

Unreliable or absent native text now retains the admitted original, available
figures, extraction assessment and immutable processing target in CAS without
model work or a knowledge publication. Actual blank inputs remain distinct from
unreadable display formats. Explicit retained-input/original/asset reads require
one exact processing revision; normal source reads stay within the bound
publication's active and historical input closure. Markdown and PDF image bytes
remain fixed after a replacement reuses the original file names.

Both review axes reproduced and verified repairs for an unsupported JSON display
shape, cancellation before accepting processing, stale native coordinates after
historical image relocation, and a stopped admission's retry bypassing the native
text gate. The last case could otherwise send two requests for a blank original;
the real application/SDK regression now retains it with zero sends after deleting
the external file. Historical PDF relocation uses the SDK's recorded per-page
spans before generating selections, preserving image/table roles and offsets.

Nine focused retention cases pass independently on both review axes. Related
compiler, workbook and ingestion checks pass 39 cases; source reading/ingestion
checks pass 34 cases. Targeted mypy and Ruff checks pass. These are development
main-store paths; explicit reprocessing, remaining writers, maintenance and P8
activation are still being completed. Issue 164 remains open.

### Explicit managed reprocessing and request-owned recovery

Development schema 16 records immutable processing requests separately from the
source revision. A preview fixes the managed original/sidecars, policy, baseline
unit revisions and publication before execution; a changed recipe or competing
request invalidates that token before model work. Explicit unchanged-policy
processing creates a new target, while resuming an incomplete request reuses its
task, cumulative budget and accepted results. Processing and workbook inventories
record their request identity. Failed inventory attempts remain diagnostic;
retry reparses the managed original instead of treating the failure as final.

Partial workbook publications can advance only along that request's own accepted
chain. Foreign publications and target changes reject stale recovery, including
generic source retry. A reviewed preview after a competing formerly-absent target
captures a new baseline rather than repeatedly returning an unusable old token.
The final unit/publication transactions recheck request ownership and exact
presence/generation. Concurrent resumes also check the latest task execution in
the write transaction; a partial unique index independently prevents two active
executions for one task.

Thirteen real application/SQLite/loopback-SDK cases cover unchanged and changed
recipes, source deletion, failed publication and parsing, native-only retention,
workbook partial chains, competing targets/requests, concurrent resumes and a
new-generation backup restore. Related regressions passed 127 tests before the
last two boundary repairs; the final focused run passes 35 tests. Independent
standards review passes 27 related cases and the spec axis verifies both final
boundaries. Full mypy passes 527 source modules; Ruff checks pass. This batch does
not activate the normal factory or complete the remaining compiler, maintenance,
observation and three-entrypoint work. Issue 164 remains open.

### Manual retirement of obsolete recovery roots

Development schema 17 adds indexed checkpoint-root lookups and compact retired
synchronization plans. Explicit root preview/retirement uses the existing
exclusive reclamation protocol; real reads return busy with zero release, while
pure model waits allow unrelated intermediate checkpoints to retire. Latest
calculation and current retrieval checkpoints remain separate roots. Immutable
user/assistant turns, current projections, knowledge/source history and paid
receipts are untouched.

Superseded synchronization plans remain protected while the current worktree
target is pending, conflicted or paused. Only after the successor completes can
bounded batches remove obsolete items and their owned Blob references. The
formal revision keeps a compact plan binding; task queries preserve supersession
and explicitly mark retired detail coverage. Released reference bytes are
reported separately from physical/unique Blob bytes; normal collection still
rechecks actual references before deleting files.

Ten application regressions use actual SQLite/SDK/provider boundaries, including
failed final ChatIndex projection and zero-send repair, predecessor replacement/
deletion recovery, model-wait concurrency, receipt ownership, fresh-reference
rechecks, item/manifest retirement interruptions and backup/new-generation
restore. Maintenance and backup also accept an unpublished main store without
model configuration. Sixty-nine related regressions pass; full mypy passes 528
source modules, with Ruff and format checks passing. Spec review independently
passes all ten new cases. Remaining archival/audit policy, maintenance adapters,
compiler migration and P8 activation are separate unfinished work; issue 164
remains open.

### Shared explicit storage maintenance in CLI, HTTP and native Qt

CLI `storage` and authenticated `/api/v1/storage` now expose the shared preview,
bounded root retirement/collection, retained-receipt repair and verified backup/
new-directory restore. Mutating HTTP requests require the reviewed KB identity
and generation; CLI accepts the same precondition pair. A native storage dialog
is reachable from maintenance and uses those same application operations through
LocalIO, without a legacy outer KB lock. Its initial identity stays fixed across
refresh and subsequent actions. The shared preview separates reference bytes
from unique physical candidates and exposes protected counts and blocked reasons.

Real Qt event delivery continues while preview waits for reclamation; closing
cancels that wait before the exclusive lease is released. Actual mutation keeps
the dialog open through I/O completion. Independent restore remains available
when the current main database cannot be opened. Successful backup/restore
receipts render before refreshing; a later read error preserves the completed
path, coverage and result instead of hiding them behind a preview diagnostic.

Seven entrypoint regressions compare CLI/HTTP/Qt against the shared result,
reject stale-generation mutations, back up and independently restore fixed
history, repair an actual retained paid response with one total model send,
and inject source-database loss after a completed backup. Forty-one related
application/adapter/size/boundary cases pass; full mypy passes 532 modules, final
targeted mypy passes nine changed modules, and Ruff/format checks pass. An actual
offscreen Qt render confirms labels and six controls fit the dialog. Both review
axes independently verify the final result-preservation and recovery fixes.
These commands do not activate normal-format creation or complete the remaining
compiler, observation, audit/archive and P8 acceptance work. Issue 164 is open.

### Compiler outcomes and processing-unit cost attribution

Development schema 18 retains the accepted publication result if subsequent
projection coordination or execution cleanup fails. A stale foreign publication
is rejected before new model sends; an already committed import still recovers
its exact accepted result. Recompilation reports the selected unit, successful
input revision and actual processing policy separately from the replacement
target. Stopping before a new execution begins does not borrow a previous
execution's current usage.

Indexed execution/unit statistic buckets attribute each worksheet's actual sends
and retained historical costs independently; root source totals remain separate.
Missing usage confirmation preserves the compiled publication and adds an
explicit confirmation diagnostic rather than inventing zero cost or failure.

Twenty-eight compiler/outcome/workbook application regressions pass using the
actual SDK and local provider. Fifty-six related execution/usage/reprocessing
regressions also pass (19 live cases deselected). Full mypy passes 533 modules;
Ruff and format checks pass. Both independent review axes find no remaining
concrete defect in this batch, including early-stop and old/new policy cases.
Normal entrypoint activation and the rest of issue 164 remain unfinished.

### Retained document management and confirmed recompilation batches

Development schema 19 adds indexed last-work outcomes for retained units. Source
and document management queries now read the main catalog and fixed publication,
show actual successful processing separately from current replacement targets,
and list compiled pages from the immutable manifest. Source enumeration uses
bounded indexed metadata pages and does not open every original or read legacy
JSON catalog files.

Selection expands retained sources into explicit units, including every eligible
worksheet. A confirmed token binds database identity/generation, the displayed
source/unit target snapshot and publication. It never incorporates a replacement
accepted while the preview is rendered. CLI/HTTP batches advance only to each
unit's own durable publication; desktop requests retain unit identities and the
existing receipt-based next-unit precondition. Intervening foreign publications
stop the next unit before a model send. Explicit schema refresh preserves its
previous bytes in the publication and advances only its own resulting baseline.

Main-store usage receipts retain identity and precise execution/source/unit
selections. Merging reads indexed buckets and overlap intersections within one
short read snapshot; parent and child costs are not counted twice. Late usage
cannot make cross-time subtraction negative, and a same-path rebuilt database
cannot silently replace an old paid receipt with zero. Failed final statistics
or next-publication confirmation retains already compiled unit outcomes and
reports the remaining work/confirmation diagnostic.

Fourteen management regressions pass at actual SQLite/SDK/loopback boundaries,
including CLI and HTTP batches, native request identity, concurrent target and
publication changes, schema refresh, exact unit totals, late receipts, generation
replacement and post-publication read failures. A related set passes 101 cases
(13 live cases deselected); full mypy passes 536 modules, Ruff and formatting
checks pass. Both review axes verify the concurrency/identity fixes and final
result retention. Normal factories remain at the P8 gate; remaining compiler
matrix, source maintenance, observation/archive/audit and final enablement are
unfinished. Issue 164 remains open.

### New Skill generation with independent main-store acceptance

The main-store development path now runs the actual Skill creator against a
fixed knowledge publication. It accepts each delivered output file independently
through the artifact catalog and writes the marketplace manifest after its
referenced Skill files. Generated output does not create a knowledge publication.
Existing target replacement and Deck/custom-skill generation remain separate
unfinished work; normal factories are still behind the P8 activation gate.

Schema 25 retains immutable generation plans with reserved per-file acceptance
identities. Recovery confirms the exact original receipts across resumed
executions. The first artifact epoch, reviewed selection and model fingerprint
are captured before paid work; an unrelated artifact update cannot silently
change the work identity and repeat accepted requests. Conflicting acceptance
retains the candidate instead of replacing a concurrent output.

Native SDK responses, reader outputs and nested paid navigation ranking responses
have separate retained checkpoints. Replaying private file writes rebuilds the
temporary output, while replaying a search returns its exact accepted text and
restores its context allocation. A received rank or reader result awaiting SQL
acceptance recovers without another provider send. The SDK's tool-error wrapper
is unwrapped only for known checkpoint/cancellation errors. Real provider errors
continue to preserve private files already delivered by successful tools.

Each successful artifact receipt returns with the known prefix of accepted
files. A later persistent database failure cannot erase those facts or require
another successful SQL query to display them. Working-copy synchronization is
reported separately. Lost plan acknowledgement, per-file acceptance failure,
lost save acknowledgement, projection failure, cancellation and later HTTP 400
all retain the appropriate paid work or accepted files.

The generator fault matrix uses actual SDK requests through a loopback provider
and real temporary SQLite/CAS, including a persistent WAL SHM availability fault.
The expanded generator/artifact/lint/retrieval/application set passes 80 cases
before the final provider-failure parameter was added; the final targeted matrix
passes 49. Both independent reviews confirm all reported findings are closed.
Full mypy passes 564 modules. Issue 164 remains open while compiler review/discovery integration,
remaining generator/management writers, observation/archive/audit and P8 remain.

### Main-store lint and link maintenance

Link repair and structural/semantic checks now read a fixed complete private
Markdown workspace backed by the main publication and retained native inputs.
Preview consent binds the database generation and publication, without a legacy
registry or whole-operation ingest/reclamation lease. Structural-only checks do
not need model configuration. Mechanical repair preserves the existing image
references and bytes; compiler image freezing retains its default behavior.

Repairs, the report and operation log publish together. The schema-24 lint
candidate record joins the exact ready revision, execution and result Blob;
published receipts determine lint history and fixed-publication status. A failed
projection keeps the committed report and exposes pending synchronization. A
replay uses the original publication. Unpublished candidates whose applied
projection epoch changed are rebuilt from retained semantic work, without paid
resends. A concurrent knowledge publication rejects the candidate instead of
overwriting the newer version.

Semantic lint preserves each accepted native SDK response and the final report
independently. Exact invocation identities connect gateway receipts to the work
step. Cancellation or a checkpoint write failure after receiving a response
recovers through the pinned SDK's native conversion, including tool calls; an
uncertain or incomplete receipt requires recovery and cannot silently resend.
Serialization preserves whether optional SDK fields were originally present.
Explicit credentials bind the actual executor and cannot replace a frozen
execution. Ordinary semantic errors retain mechanical repairs and a report with
an explicit unfinished semantic check. CLI, HTTP and a real native spawn worker
expose the committed/pending outcome consistently.

Twenty main-store lint cases and the expanded maintenance/publication/figure/
usage/size/boundary set pass 68 checks. Mypy passes 559 modules; Ruff and diff
checks pass. Model-wait witnesses allow unrelated orphan collection and reject
late publication conflicts. Fault witnesses cover report and SDK acceptance,
cancellation, publication and projection, as well as the real applied-epoch race.
Both independent reviews confirm their findings are closed; the final unknown
usage witness preserves the exact report text with one actual provider send.
The generators, remaining management connections, observation/archive/audit work
and P8 activation are still unfinished; normal factories remain behind the sole
activation gate and issue 164 remains open.

### Durable compiler diagnostics and capacity recovery matrix

Development schema 20 binds a validated, bounded compiler outcome to each import
claim and sealed revision manifest. Failed work retains actual quality codes and
unfinished stages; an exhausted repair does not become a generic empty failure
on the next run. Completed outcomes survive idempotent publication recovery and
report quality availability explicitly. Accepted repair facts are recovered only
from this work's repair claims joined to accepted steps; resolved incomplete-work
codes can clear without erasing a paid, successful repair.

Candidate preparation is extracted from the formerly 797-line KnowledgeStore
into its own module. Review compares the moved workflow structurally and finds
no omitted capture/seal/verification behavior. Payload encoding and sealed-object
validation remain outside SQL writer transactions. Compiler-incomplete results
now report failed with their specific diagnostics, matching the existing shared
compiler contract; raw storage interruption remains blocked and recoverable.

Real SDK cases exercise a 48-page catalog under a 24,000-byte input guard,
retained partition acknowledgement failure, exhaustive catalog identity scanning,
large description fragments and persisted reasoning compaction. Restart resumes
accepted work without repeating any already sent request or truncating the
existing catalog. A repaired plan survives both accepted-step acknowledgement
loss and final publication failure with three total sends. Execution-finish
acknowledgement failure retains the already determined failure and appends only
cleanup confirmation; publication success immediately fixes the business outcome.
A later compiler-quality read failure preserves the successful publication with
quality_known=false.

Ninety related application/publication/maintenance/size/boundary regressions pass;
the final diagnostics and cancellation set passes 13 cases. Full mypy passes 538
modules; Ruff, formatting and diff checks pass. Both independent review axes
verify accepted-repair recovery, bounded persistence and cleanup-result retention.
This is further compiler migration coverage, not overall completion or P8
activation. Issue 164 remains open.

### Shared-page coordination request identity

Six real SDK/main-store cases retain all 24 original paragraphs while updating
separate editing groups and coordinating conflicting metadata under a 24,000-byte
input guard. Interruption after the first or second accepted update resumes from
the saved steps without repeating any already sent request. This exposes a real
request-identity defect: canonical checkpoint JSON changed nested proposal key
order after restart, changing the later coordination signature. Main-store
CompileWork explicitly declares canonical drafts; both coordination contexts
reproduce their canonical order. Legacy checkpoints, including protocol v3, keep
their frozen request bytes. Independent review's legacy-v3 upgrade witness also
passes without a repeated accepted step.

The related update/plan/compiler set passes 36 cases; shared-update and existing
reconciliation recovery pass 28 cases. Full mypy passes 538 modules; Ruff and
format checks pass. This closes this shared-page recovery gap, not the complete
issue or the P8 gate.

### Source withdrawal and confirmed batch recovery

Development schema 21 records generation-bound source withdrawal in the sealed
revision and commits the catalog transition with its publication receipt. Existing
knowledge bytes, exact page evidence, originals and paid work remain retained.
Source/unit changes and foreign publications invalidate confirmation; pending
imports and following compiler/index requests stop before another model send.
An already received response and its actual usage remain independently durable.

Single and batch removal share this application operation across the development
CLI, HTTP JSON/stream and native worker bindings. Confirmed tokens bind the original
publication and source generation. A fresh invocation can recover that exact
receipt after a lost response; batch recovery proves its contiguous committed
prefix, resumes the remainder and rejects a foreign publication. Nonmodel source
withdrawal needs no model configuration or credentials. Postcommit coordination
and projection failures retain known business success with separate remaining
work. A superseded synchronization plan is terminal, so replay never asks to
permanently resynchronize an obsolete publication or rolls back a successor.

The expanded storage/application/legacy compatibility set passes 239 cases (one
live case deselected); the final withdrawal/batch/three-entrypoint set passes
22 cases. Full mypy passes 541 modules; Ruff, formatting and diff checks pass.
Both independent review axes verify the generation checks and receipt recovery;
Spec independently confirms the obsolete-plan regression. Normal factories stay
behind the P8 gate. Refresh/maintenance completion, remaining observation work
and final entrypoint activation are still required; issue 164 remains open.

### Knowledge validity and committed status

Main-store evidence compares the selected page's actual immutable unit inputs with
current source and processing targets in a short read snapshot. Updated or withdrawn
inputs mark dependent pages for refresh without changing their bodies, digests or
source revision references. A newer unrelated publication does not invalidate an
otherwise current basis. A fresh question captures validity again even when its
fixed resource inventory was already cached; per-page reads report that page's
actual basis. Legacy current and explicit historical selections retain their
existing distinction.

The status use case counts committed main-store resources and managed sources,
ignoring external worktree drafts. Compile timestamps come from actual import
publication receipts in commit order; missing compile/lint observations remain
unrecorded instead of using file modification times as evidence. Five main-store
cases and a real legacy publication regression cover these contracts. The related
compiler/question/document set passes 87 cases (two live cases deselected); the
final validity/page-write set passes 17. Full mypy passes 543 modules and Ruff and
format checks pass. Both review axes close the fixed-reader and legacy validity
findings. The refresh writer and P8 activation are still unfinished.

### Main-store full refresh and durable review

Development schema 22 adds a catalog generation fence, immutable generated-page
baselines and refresh claims/outcomes to sealed revisions. Catalog source/unit
transitions advance the fence in their own transaction; publication checks one
row before committing. Generated baselines retain their exact Blob references
through manual edits. A full refresh rebuilds every eligible retained unit with
the existing short/long compiler and independent main-store checkpoints, reuses
ready shared indexes and commits one complete publication. Withdrawn inputs are
excluded; an empty rebuild requires neither model configuration nor credentials.

Manual summary/concept/entity/report/exploration and root navigation changes
produce durable candidates and complete review diffs. Existing custom AGENTS.md
rules remain the actual compiler input; a missing default seed is not assigned
source evidence. Exact review versions bind the database generation and candidate.
Source/catalog or head changes reject approval, while acceptance/replay preserve
the exact committed result. Candidate lists read bounded metadata rather than
building every diff. Recent compile time includes real compiler refresh receipts;
empty refresh does not invent a new model compilation time.

Actual SQLite/SDK cases prove independent accepted-summary recovery and retained
candidate recovery after publication failure, with no paid request repeated.
Source, new unit or withdrawal changes during model wait stop the next send.
Lost publication acknowledgements, projection failures and later observation or
execution-cleanup errors retain known business success. A real pending worktree
sync can change its applied epoch after candidate preparation: retry reconstructs
only a new candidate/plan from accepted compiler steps. Manual changes still need
a new exact review token. Existing historical knowledge and originals remain roots.

Nineteen refresh cases and three CLI/HTTP/native development-binding cases pass.
The expanded storage/compiler/publication/backup/maintenance set passes 137 cases;
the final refresh/validity/page-write/size/boundary set passes 45. Full mypy passes
548 modules; Ruff, formatting and diff checks pass. Both independent review axes
verify the custom-schema, root-navigation, timestamp and synchronization-reprepare
fixes. Source empty confirmation, selected refresh, remaining maintenance writers,
observation/archive/audit completion and the sole P8 normal-factory activation
remain open work; this is not overall completion of issue 164.

### Empty contribution confirmation and target fencing

Development schema 23 records empty contribution and withdrawal through the same
sealed source-change claim. Empty confirmation atomically publishes its exact
source revision and generation, keeps originals and historical page evidence, and
marks dependent knowledge for refresh. New effective inputs exclude the source.
An exact generation-bound receipt recovers the same publication in a fresh
invocation; a changed source, unit or foreign publication rejects approval.
Confirmation and replay need no model credentials or configuration.

Ordinary normalization, retained-only processing and worksheet preparation bind
the source and unit generations captured before their work. Target advancement
checks those generations inside its short transaction, and an ordinary late worker
cannot erase a committed empty confirmation. Workbook planning advances its local
expectation only for its own actual target changes. Explicit reprocessing records
the reviewed source generation and advances its owned progress in the same
transaction as each target. Empty confirmation invalidates an old request; only a
fresh preview can authorize contribution restoration.

Real SQLite/SDK regressions cover late normalization, fresh reviewed reprocessing,
confirmation during model wait, stale pending work and retained paid responses.
Transaction abort, lost acknowledgement, projection failure and later execution
cleanup preserve the correct durable result. Development CLI, HTTP and native
worker invocations recover one exact receipt. The desktop asynchronous review
captures the fixed main-store reading scope even if navigation changes before
delivery. Both independent review axes verify their earlier findings are closed.

The expanded catalog/import/reprocessing/refresh/removal/application set passes
109 cases; the final adapter and application-boundary set passes 8. Full mypy
passes 549 modules, and Ruff, formatting and diff checks pass. Normal factories
remain behind P8. Selected refresh, remaining maintenance writers, observation
and archive/audit completion, and final activation remain unfinished; issue 164
is still open.

### Bounded knowledge history and fixed historical assets

Main-store history lists bounded committed receipt metadata in publication order,
with a fixed publication upper bound and explicit cursors. Revision reads accept
only committed publications in the same view that do not exceed that bound;
ready unpublished candidates are not history. Pages load separately in bounded
batches, and explicit history reads capture the page and image bytes from one
immutable publication. Worktree drafts and newer images cannot replace them.

CLI and HTTP expose the same list, cursor and fixed-publication selection. Unknown
revisions/cursors and invalid page cursors return explicit 404/400 responses.
The desktop loads revisions and pages on demand and follows links in the selected
revision. Its main-store I/O bypasses legacy directory binding; fixed storage
identity and a cancellable actual-read lease protect the shared reader. Closing
or changing the selection cancels a wait for the reclaim lease. Legacy explicit
historical/public selections retain their requested history starting point.

Five history cases pass, including a real Qt/LocalIO test that displays a retained
page and cancels a blocked read before an exclusive GC lease is released. Both
independent reviews verify the HTTP and desktop findings are closed. The expanded
reading/history/publication set passes 60 cases and the final updated set passes
55; full mypy passes 551 modules, with Ruff, formatting and diff checks passing.
Selected refresh and the remaining writer/observation/maintenance/P8 work are
still pending, and normal creation remains behind the sole activation gate.

### Selected-source refresh with retained compiler work

Selected refresh now uses the main-store source targets, accepted unit revisions,
page evidence and catalog fence. Independent generated summaries retain their
exact bytes and need no model configuration or additional requests. Shared pages
use the existing complete-original window reader, block-edit coordinator, manual
fragment preservation and bounded recovery algorithm. Conversion, indexing,
document summaries and planning are not rerun. Other pages and their pending
evidence remain retained, and explicit page limits keep the root index pending
until it is actually refreshed.

Selected sources and page sets bind both checkpoint and candidate identities;
reversing a page set cannot create new paid work. Accepted page steps and prepared
candidates recover independently after delivery or publication failure. Human
annotations remain protected and changed pages require exact explicit review.
Catalog/head changes during model wait reject publication while preserving the
paid response. Partial outcomes and remaining page identities survive review,
acceptance and replay. CLI, HTTP and the native worker expose the same source and
page parameters and preserve partial results.

Preflight and execution share the same reconciliation eligibility. A page with
an unselected changed contributor stays pending even if a manual frontmatter edit
removed that association. Every selected old unit basis requires an accepted
replacement before paid coordination; an unfinished sibling unit cannot trigger
a late KeyError or silently lose its pending evidence. A deterministic selected
contributor owns the reader, checkpoint binding, stage and actual request usage,
rather than assigning cost to an unrelated contributor by random revision ID.

Seventeen selected-refresh cases cover these contracts with real SQLite/CAS and
SDK requests. The expanded selected/full refresh/history/page/adapter set passes
65 cases; the final recovery/attribution matrix passes 6. Both independent reviews
verify all reported findings are closed. Full mypy passes 553 modules, and Ruff,
formatting and diff checks pass. Remaining maintenance writers, desktop management
connections, observation/archive/audit work and the sole P8 activation are still
unfinished. Issue 164 remains open.

### Retained compiler candidates and explicit import review

The main-store compiler now retains complete import candidates independently of
publication. Schema 26 owns immutable review metadata and exact candidate versions.
Changes to human-edited generated pages require explicit review; manually deleted
generated pages retain their original baseline marker and cannot be silently
recreated. The publication transaction checks the reviewed version. Ordinary
reentry returns the same pending review, and a concurrently accepted candidate
recovers its exact committed receipt before considering old conflict metadata.

Review acceptance uses no model configuration or provider requests. Per-unit
accepted results include their knowledge revision, while the operation reports
its exact publication and working-copy synchronization separately. Lost commit
acknowledgement, failed projection and persistent database failure after a
successful commit preserve the known saved result. Failed preparation remains
recoverable from the accepted compiler outputs.

When only the applied worktree baseline changes, retained files, inputs, generated
baselines and compiler outcome prepare a fresh candidate and synchronization plan.
Ordinary compilation resumes without another SDK send. A human review receives
the new candidate and confirmation version; the old version cannot publish it.
Repeated preparation reuses the same ready derivative instead of accumulating
equivalent candidates.

Proposal lists have bounded metadata pages and explicit continuation. CLI exposes
`--after`, HTTP exposes `X-UltraKB-Next-After`, and Qt loads additional pages and
the selected complete diff on demand. The library's complete-list operation walks
bounded pages. Superseded entries cannot conceal older current reviews. The
desktop captures the main-store `read_scope`, bypasses the legacy read lease and
cancels a read waiting on reclamation when the dialog closes.

The expanded compiler/proposal/lint/refresh/application matrix passes 105 cases.
The final updated Qt paging/cancellation witness passes with the real Workbench
scope shape. Both independent reviews confirm all reported findings are closed;
full mypy passes 567 modules, and Ruff, formatting and diff checks pass. These are
actual SDK loopback requests, SQLite/CAS, spawned worker and Qt/LocalIO tests.
Embedded discovery, worksheet retirement, remaining generators and management
connections, observation/archive/audit and the P8 activation remain unfinished.
Issue 164 is still open.

### Worksheet retirement from complete retained inventories

Schema 27 binds an optional exact worksheet target to the existing immutable
source-change claim. Complete inventories plan empty and absent worksheet targets
before any model work. Their exclusions commit with the knowledge publication
and receipt; source generation, latest original, exact unit target and current
reprocessing request are checked in the final short transaction. An incomplete
or failed inventory cannot establish absence. Whole-source exclusion remains
separate from an individual worksheet exclusion.

Retired worksheets keep their stable unit identity, managed original, processing
history and prior knowledge basis. Source lists report empty/retired targets
beside the actual successful revision. Refresh skips only committed exclusions
for the latest source revision. A later source admission cannot inherit an older
revision's retirement as a current exclusion. Full empty-workbook refresh needs
no model settings, sends no requests, and retains old-publication readability.

Repeated source and selected-worksheet retry recover the exact committed receipt
without resending successful sheets. Publication acknowledgement loss, projection
failure, stage-observation failure and SQL rollback are covered. A newer original
admitted immediately before publication rejects the old retirement. Combined
workbook results report the last publication and preserve synchronization pending
from any accepted unit.

The expanded worksheet/source/refresh/publication/application matrix passes 118
tests; mypy passes 568 modules and focused Ruff/format/diff checks pass. Independent
readonly reviews verify real SQLite/CAS, SDK loopback and persistent postcommit
database failure behavior. Embedded discovery and pending-work application flows,
remaining generators and management connections, observation/archive/audit and
the P8 activation remain unfinished. Issue 164 remains open.

### Frozen embedded discovery and shared pending execution

Schema 28 retains discovery inputs, object diagnostics, exact source bindings and
delivered source-import results in the main database. Discovery reads the managed
original even after its external path disappears or its parent contribution is
withdrawn. Each accepted object commits its cursor, payload reference, child
intent and source/byte counters together. Model work starts only after the child
is independently admitted. Equal bytes at different container paths remain
different sources; recursive imports share one frozen execution-group budget.
Explicit reprocessing adds its request identity to discovery and object identity.

The existing OOXML/compound recognizers and format policies are reused. Actual
decompression and elapsed discovery work remain recorded after failures and
budget waits. Raising a retained budget resumes the same cursor; cancellation
does not reset accepted objects or paid work. Long ZIP object paths use bounded
stage keys, and long recovered filenames retain their display labels while
materialization uses bounded physical filenames. Frozen view identities are
validated before use.

Pending dispatch starts one execution for its exact token and generation. Unit
claims bind their active execution; a retained candidate can transfer only after
the preceding execution terminates. Stops fence the exact attempt, prevent later
sends/publication, and retain responses and actual usage received during cleanup.
Known source-import publications survive result-receipt and observation failures;
explicit retry reuses accepted compiler work instead of sending it again.

CLI, HTTP and the spawned native worker use the shared pending operations through
development main-store factories. Lists and selected object diagnostics have
separate bounded cursors. Metadata reads need neither model settings nor a Wiki
working copy. Qt captures its read scope, exposes diagnostic tails and cancels
storage waits when its panel closes. Native stopping sends control immediately
and confirms persistence off the Qt thread; shutdown waits for that confirmation.
Unconfirmed stop deliveries remain visible across restart and pause resident
automatic dispatch until explicit confirmation. They cannot be cleared or used
to retry model work, and repeated confirmation clicks cannot duplicate an active
control write. HTTP cancellation waits for actual worker and provider cleanup.

The expanded discovery/compiler/reprocessing/worksheet/native/adapter matrix
passes 115 tests, with 10 live cases deselected. Full mypy passes 576 modules;
focused Ruff, formatting, module-size and diff checks pass. Independent readonly
reviews reproduce and close the transaction, concurrency, paging, long-name,
budget, view and real Qt/GC stop findings. Remaining generator replacement/deck
and management flows, observation/archive/audit work and the sole P8 activation
are still unfinished. Issue 164 remains open.

### Retained Skill replacement and explicit artifact removal

Schema 29 distinguishes immutable artifact writes from explicit deletion records.
Removing a current artifact preserves its earlier epochs and revision identities.
Bounded file cursors advance over deleted names, including empty pages. A missing
first entry cannot hide the remaining files of an existing generated directory.
Projection retries synchronize the existing parent chain even after an earlier
unlink; persistent directory synchronization failures remain pending. Unknown
external edits are preserved by the same physical-baseline checks as writes.

Skill overlay reconstructs the selected retained directory before generation,
preserving untouched references. Archive prepares a separate retained plan,
accepts all copies before any removal, and finishes before further model work.
Interrupted removal resumes the same archive plan and model budget. Structural
diff reports reuse the existing Skill diff writer. A conflict before the first
copy reports no accepted archive, and independent archives do not claim that
unrun validators passed.

Main generation replay also retries pending archive synchronization without
model configuration or another SDK send. Archive synchronization preserves the
main generation's actual execution outcome. Response checkpoint failures after
archive report blocked recovery while retaining accepted archive facts; a real
model failure remains failed in both the public result and the execution ledger.

The focused replacement matrix passes eight actual SDK-loopback/SQLite/CAS
cases, including persistent directory I/O faults, SQL rollback, concurrent CAS
conflict, exact old-epoch reads and response recovery. The expanded generator and
artifact matrix passes 57 tests; mypy passes 577 modules and Ruff, formatting,
module-size and diff checks pass. Independent readonly
reviews reproduce and close the paging, durability and execution-state findings.
Deck/custom-skill generation and remaining management, observation/archive/audit
work and the sole P8 activation are still unfinished. Issue 164 remains open.

### Retained Deck producer, critic and exploration publications

Schema 30 binds a generated-output plan to an optional exact knowledge candidate.
Deck production and optional critique use the real SDK with separate retained
compile work, one original execution budget and fixed knowledge readers. Producer
and critic definitions are frozen before use. Received responses reconstruct the
private files on retry; a critic checkpoint interruption reuses the paid response
and continues the same cumulative budget even if the local Skill file disappears.

Custom templates retain the existing file/directory selection rules and allowed
output zones. The main-store path validates logical names without resolving the
mutable Wiki or output projection. Unknown symlinks can leave projection pending,
but cannot redirect accepted content or prevent its main-store acceptance. Other
existing artifacts and archive versions remain protected from generator tools.
The private writer revokes tool access before its temporary materialization exits.

Output artifacts commit separately from exploration publication. A failed note
publication retains accepted artifacts and its exact candidate; retry publishes
that candidate without another SDK send. Publication and synchronization receipts
remain distinct, including replay without model settings after a projection
failure. Generated exploration pages and ordinary output files support explicit
archive and overlay. Archives retain the original Markdown's exact input revision
basis, and unchanged overlay pages preserve that basis as well as their bytes.

The expanded generator/artifact/Skill/Deck matrix passes 105 tests with two live
cases deselected. Two focused final witnesses also pass for retained cumulative
budget and protection of existing artifacts. Mypy passes 580 modules; focused
Ruff, formatting, module-size and diff checks pass. Independent readonly reviews
close the logical-path, archive-basis and late-tool findings. Remaining management
connections, observation/archive/audit work and the sole P8 activation remain
unfinished. Issue 164 remains open.

### Bounded refresh review and actual reader cancellation

Refresh review lists now page over retained proposal metadata in the selected
view without loading every diff. CLI and HTTP expose the same cursor, and Qt
loads the complete selected diff before enabling acceptance. The dialog captures
its main-store identity, reads the current publication on refresh, and keeps the
selected review version fixed. A real withdrawn source verifies that refresh
reasons and a review beyond the first 63 superseded entries remain accessible.

Both HTTP list and full-diff reads signal cancellation through the storage lease
and join the actual reader before returning. The ASGI witnesses hold a real GC
exclusive lease throughout cancellation; they fail if the request abandons its
blocked reader. Closing the Qt panel likewise stops its queued storage read.

The expanded refresh/entrypoint/history/desktop matrix passes 51 tests, with 39
live cases deselected. After extending cancellation coverage to the full-diff
endpoint, all three focused review cases pass. Mypy passes 580 modules; focused
Ruff, formatting, module-size and diff checks pass. Remaining artifact management,
observation/archive/audit work and the sole P8 activation remain unfinished.
Issue 164 remains open.

### Retained desktop artifact browsing and browser dependencies

The artifact workbench captures the main-store read scope and pages over a fixed
output epoch and knowledge publication. Selected files, Markdown images and
complete exports use those retained originals even after newer output is
accepted. Unknown physical images cannot fall back to Qt's file reader, including
Markdown files with uppercase extensions. Directory selection captures the
artifact and version before entering a modal event loop, so a concurrent refresh
cannot destroy or change the export selection.

Browser previews use a short-lived loopback read service bound to the same
publication and output epoch. Relative dependencies, including another retained
package or a sibling of a single HTML file, are read on demand through actual
storage stages. Idle previews hold no reclamation lease. File-URL dependencies
are refused before opening the browser; unknown local paths never read the
working copy. No temporary artifact directory is created or cleaned up.

The I/O owner accepts each preview before queued UI delivery. Obsolete results,
selection changes and application shutdown stop the appropriate services.
Joining actual readers happens off Qt and remains included in the application's
I/O shutdown wait. Blob reads check cancellation between 1 MiB chunks without
changing complete integrity validation for normal exports. A real blocked CAS
write proves GC remains busy, the GUI stop returns before that I/O completes,
and shutdown remains pending until the actual reader exits.

The expanded artifact/desktop/boundary matrix passes 29 tests. Six focused final
cases also pass for retained HTTP dependencies, refused file references and the
real Qt paging/image/export/cancellation matrix. Mypy passes 581 modules; focused
Ruff, formatting, module-size and diff checks pass. Generation adapter receipts,
remaining observation/archive/audit work and the sole P8 activation are still
unfinished. Issue 164 remains open.

### Shared generation receipts and actual HTTP cleanup

Skill and Deck generation now expose one validated JSON receipt with accepted
resources, archive and candidate facts, publication identity, output epoch,
validation, pending synchronization and independently recorded model usage.
CLI JSON, HTTP JSON/SSE and native unit receipts retain the same schema. A failed
note publication preserves the accepted artifact and exact candidate in every
adapter; failed projection preserves completed business success. Native results
also populate their ordinary usage and publication fields for existing task
consumers. Usage reads reuse the shared confirmation boundary and cannot revoke
already accepted business facts.

CLI `skill new` and `deck new` support JSON output. Pending copies are described
as accepted main-store results, and Deck validation errors retain the existing
nonzero exit status in both plain and JSON modes. Legitimate scope-resolution
failure becomes a normal SSE error followed by completion, rather than raising
after response headers. The public view shape remains restricted to `legacy`.

The existing application event/cleanup implementation now also serves generation.
Streaming HTTP polls disconnection while awaiting the next event; cancellation
and repeated cancellation join actual SDK and worker cleanup before releasing
the operation lock. Real delayed provider responses retain their actual usage.
GC can still complete during a model wait, and no unaccepted artifact is exposed.
The non-streaming ASGI request likewise waits for actual cleanup after cancellation.

The expanded generator/entrypoint/answer-stream/CLI matrix passes 102 tests with
26 live cases deselected; the final additional non-streaming cancellation witness
also passes. Actual spawned native workers cover both projection failure and SQL
publication rollback. Mypy passes 584 modules; focused Ruff, formatting,
module-size and diff checks pass. Independent readonly reviews close the native
usage, scope-error and validation-exit findings. Remaining P6 task association and
drilldown, P7 archive/audit/retirement and the sole P8 activation are unfinished.
Issue 164 remains open.

### Native batch identity and durable budget continuation

Schema 31 retains the application task, work-unit ordinal and retry parent on
each execution. The ordinal is separate from a domain processing-unit identity.
Task detail and JSON exports expose these associations through the existing
shared query schema. Model-budget bindings and execution creation commit in the
same transaction; their lookups use exact keys and a budget/execution index.
Non-model operations record their association without claiming a model budget.

New work in one native batch shares its retained budget across spawned workers.
A retry parent preserves that budget and its original absolute deadline. Source
retries bind their individual original budgets to the corresponding ordinal,
so one selected batch can continue sources from different original tasks while
new work shares a separate batch budget. An already bound ordinal cannot change
its budget, and concurrent starts cannot allocate two budgets for the same batch.
Budget exhaustion is reported as blocked without discarding accepted outputs.

The real spawned-worker/SDK witness first reproduced four actual sends under a
two-call batch limit, then passed with exactly two sends and zero sends in the
next worker. Six focused cases cover same-task and parent-task continuation,
concurrent starts, policy/budget replacement rejection, non-model association,
mixed source budgets and preserved deadlines. The wider initial regression passes
123 tests; the final continuation/usage/task/entrypoint/size matrix passes 44.
Mypy passes 584 modules. Independent static reviews found and closed the mixed
source retry issue. This completes this association batch; source/work-item
drilldown, observation/archive/audit/retirement and P8 activation remain pending.
Issue 164 remains open.

### Retained task inputs and exact stage drilldown

Schema 32 adds an index for exact stage/request cursor queries. Task stage pages
describe the bound retained original and processing unit, including original
name, digest and byte count, source ordinal and processing fingerprint. They read
the retained manifest rather than current files or original bodies, and reuse
descriptions within each bounded page. Unavailable descriptions remain explicit
without hiding the underlying stage or independently known costs.

The shared stage-detail use case returns one exact stage, its directly associated
request page and separately scoped input usage for this execution and history.
Those input totals are labelled as source or processing-unit costs. Requests are
not aggregated into a misleading single-stage total. CLI `tasks stage`, HTTP
stage/detail export and the Qt task browser use the same schema and cursors.
The desktop groups the current stage page by input and processing unit, preserves
available parent-stage/work-item relationships and exports the selected detail.

Task HTTP reads signal cancellation and join the actual blocking reader. A real
reclamation lock witnesses both task-page and stage-page cancellation. Another
real lock reproduces and verifies the fix for a late Qt stage result after the
user returns to an input group: obsolete callbacks cannot replace the overview
or make its export disagree with the display.

The final task/native/compilation/workbook/boundary/size matrix passes 47 tests.
Mypy passes 585 modules; focused Ruff, formatting and diff checks pass. Both
readonly review axes close the selection-race finding. Remaining non-model
observation coverage, worksheet-specific failure descriptions, archive/audit/root
retirement and the sole P8 activation are unfinished. Issue 164 remains open.

### Actual non-model storage costs and worksheet descriptions

Schema 34 retains worksheet labels with their processing-unit revision and adds
optional, versioned stage observations. Actual worksheet validation failures now
have durable failed stages, while successful worksheets continue independently.
An external workbook rename or deletion cannot rewrite historical sheet names.
Admission observations retain a temporary input identity, name, digest and known
byte count; worksheet normalization records known cell counts with their unit.

Writer attempts, acquisition waits, lock holds and commit/rollback/error outcomes
are measured at the existing transaction boundary. Blob reads, confirmed writes,
deduplication hits and errors are measured at real payload operations, including
streamed private compiler inputs. Failed private copies preserve the known read
position. Nested observed scopes are exclusive, and stage-ledger bookkeeping is
excluded from those costs. These are instrumented payload and writer costs, not
physical disk usage, all filesystem traffic or an inclusive sum of nested work.
The original private copy validation and durability behavior are preserved.

Optional observations commit with the existing stage start/finish transactions.
Disabling them retains core stages, budgets and request records. Interrupted or
invalid optional observations remain partial without inventing final costs or
hiding known input usage. CLI, HTTP and desktop stage details use the same typed
payload and coverage states; the desktop labels the scope and known byte counts.

The actual parser calibration uses a five-sheet XLSX with 25,000 cells and
149,020 input bytes, one warmup pair and seven alternating measured pairs. Both
modes retain core execution/stage commits and produce identical parsed output.
Python 3.12.13, SQLite 3.53.1 and openpyxl 3.1.5 on this Linux host yield median
536.57 ms without and 543.00 ms with optional observations: +6.42 ms / +1.20%.
Observed p95/max are 547.05 and 558.47 ms respectively (seven samples per mode).
This meets the initial 5% target for this host/workload only. Reproduction is
`scripts/verify_storage_observations.py`; exact samples and environment are in
`issue-164-observations-benchmark.md`.

Expanded picture regressions exposed an existing import-review mismatch: moving
an unchanged manual page's image URL to the same retained bytes unnecessarily
required review. Review now compares structured text/media/digest segments;
literal unresolved URLs cannot collide with content markers, and unsupported or
different media types cannot qualify as equivalent. Actual manual text/image
updates still require explicit review and acceptance without new model sends.
Real SQLite/CAS witnesses first reproduce both marker-collision directions and
the reader-unsupported suffix case, then pass after their respective repairs.
A retained-input assertion now checks the factual sentence rather than a short
number that could occur randomly in a generated source identity.

The expanded storage/task/compiler/review/boundary matrix passes 107 tests before
the final shared-media refinement; its reader/picture/protection/boundary/size
matrix then passes 45 tests. Mypy passes 589 modules. Both readonly review axes
close their concrete findings; focused Ruff, formatting and diff checks pass.
Remaining P6 identity/phase coverage,
observation retention, aggregate audit, obsolete-root retirement and the sole P8
activation remain unfinished. Issue 164 remains open.

### Atomic admission-stage input association

The source admission use case now passes its actual durable stage identity to
the catalog. A new original becomes ready and binds that stage to its source
revision in the same existing transaction. Reusing identical content binds its
existing ready revision in a short conditional transaction. The boundary rejects
inactive stages, another generation and conflicting input associations.

The optional temporary-input descriptor and the retained original can therefore
be traced through the same stage. A real transaction-boundary failure after
commit demonstrates that a lost admission acknowledgement cannot hide the
original from task details, including after the external file is deleted.
Repeated unchanged inputs keep one source revision and distinct admission stages
and temporary identities, with no extra SDK sends.

An admission-only association does not replace the source's processing/budget
recovery owner. The real two-request fixture first reproduces selection of a new
admission-only task after a duplicate-input acknowledgement loss, then verifies
that source retry still selects the original processing owner and paid budget.
Actual new processing tasks retain their existing selection behavior.

The final identity/task/catalog/retry/maintenance/boundary/size matrix passes 49
tests; mypy passes 589 modules. Both static readonly review axes pass. Focused
Ruff, formatting and diff checks pass. Broader P6 phase coverage, P7 maintenance
completion and the sole P8 activation are still pending; issue 164 remains open.

### Explicit usage audit, guarded rebuild and callback WAL reuse

Development schema 35 adds one usage projection revision. Actual request changes
advance it in the same existing short transaction as their counter deltas. The
explicit audit reads one SQLite snapshot, validates core request identities and
payloads, and reconstructs every aggregate/stage bucket outside the writer.
Ordinary callbacks and task summaries retain their scoped incremental queries.

Repair prepares at most 128 differences / 256 KiB before its writer transaction,
rechecks the projection revision and advances it with the repaired counters.
A late actual receipt or another repair rejects a stale snapshot. Malformed core
records and totals outside SQLite's integer range report unavailable and do not
change requests, provider field versions, accepted responses or model budgets.
Audit and repair return the statistics snapshot version; a successful repair also
returns its applied version. CLI, authenticated HTTP and native Qt call the same
application operations. HTTP cancellation joins its actual storage worker.

The callback calibration exposed a connection-lifetime problem: retaining WAL
files while closing every SQLite connection between short callbacks caused WAL
growth and repeated open/validation cost. In a same-host, unprofiled differential
probe of 200 known-unsent request pairs, a local idle connection reduced elapsed
time from 4.578 to 1.950 seconds and WAL bytes from 14,127,512 to 4,152,992. This is
a storage-boundary observation, not an SDK or network throughput measurement.

Both existing application execution scopes now keep a local read-only connection
until operation cleanup completes. It holds no SQL transaction or reclamation
lease, stays in its creating thread and is closed inside the lifecycle lease.
There is no connection pool, cross-process connection or weaker durability rule.
The real application regression first fails at 20,970,832 / 12,516,592 WAL bytes
for model / non-model scopes, then passes default checkpoint reuse after the fix.
Actual SDK model-wait retirement and fixed-publication question regressions pass.

The final focused audit/adapter/lifetime/question/maintenance/boundary/size matrix
passes 48 tests; mypy passes 590 modules. Both static readonly review axes close
their findings; focused Ruff, formatting and diff checks pass. Scaled request
calibration is recorded separately. Observation retention, remaining obsolete
roots and the sole P8 activation remain open gates; issue 164 is not complete.

### Reversible task-list cleanup without erasing costs

Development schema 36 adds a partial visible-task index to the existing hidden
summary flag. The shared visibility operation rejects active executions and
updates only that flag. The normal bounded list omits hidden tasks; explicit
inclusion, direct detail, requests, publications, budgets and recovery remain
available. Continuing the actual original execution/budget restores visibility
in the same transaction that creates its new execution.

CLI hide/restore and optional generation guards, authenticated HTTP with strict
boolean and required generation, and native Qt background actions use the same
operation. Qt can include hidden tasks and restore them without reloading the
whole ledger. A stale generation cannot change the old task.

The actual two-request import fixture verifies unchanged paid usage, budget,
responses and publications through hiding/restoration, plus continuation and
active-task rejection. The final visibility/adapter/native-budget/database/usage/
boundary/size matrix passes 42 tests; mypy passes 590 modules. Focused Ruff,
formatting and diff checks and both static readonly review axes pass. Remaining
observation retention, obsolete-root and final activation work continues.

### Manual 90-day observation detail archive

The shared maintenance preview now reports optional stage-observation candidate
and protected counts/UTF-8 payload bytes, the 90-day cutoff and the bounded next
batch. Only a task whose latest execution completed before that cutoff, with no
active execution, qualifies. Recent, failed, cancelled and interrupted tasks
retain their optional detail. Nothing expires automatically.

Explicit archive streams candidate selection outside the writer, bounds each
batch to 128 rows / 256 KiB including parameters, and compares the original
observation while rechecking current task eligibility inside one short writer
transaction. It clears only optional JSON and marks coverage retired. Core stage
identity/state/timing, source associations, model requests, budgets, responses,
accepted results and publication records remain intact. Reported bytes are
retired inline payload, not physically reclaimed database space.

CLI, authenticated generation-guarded HTTP and native Qt invoke the same use
case. Task and stage views explain archived detail and retain core usage; the
JSON schema reports retired coverage and its reason. HTTP preview and archive
now propagate cancellation to actual storage waits and join the reader worker.

Real two-request import fixtures cover bounded batches, protected unfinished
tasks, unchanged paid records, a real continuation before the archive writer,
and a second-row SQL failure that rolls back the entire batch. Three actual
ASGI cancellation cases confirm cleanup during an exclusive reclamation lease.
The final archive/maintenance/audit/visibility/root/boundary/size matrix passes
47 tests; mypy passes 591 modules. Ruff, format and diff checks and both static
readonly review axes pass. The 30-day/512 MiB optional diagnostic policy,
remaining obsolete roots and sole P8 activation are still unfinished.

### Manual optional compiler-diagnostic archive

Development schema 37 records each diagnostic's actual execution and capture
time. Explicit maintenance applies a 30-day / 512 MiB diagnostic-payload target;
unfinished tasks, latest response pointers, accepted response references and any
unpublished knowledge revision remain protected. Unsupported, missing or
malformed accepted metadata blocks archiving conservatively. Its version and
complete response-reference shape, identity and digest are validated strictly.

Selection runs outside the writer. A batch retires at most 32 diagnostic records
(at most 128 SQL row mutations), bounds encoded parameters to 256 KiB and payload
references to the caller's byte budget, then rechecks eligibility and the latest
execution sequence inside its transaction. A concurrent continuation invalidates
the old selection. Provider responses, paid usage, budgets, accepted calculations
and publication records remain intact. Retired diagnostic reference counts and
bytes remain visible in task coverage. Unique logical diagnostic payload and
released reference bytes are distinct from physical disk reclamation; ordinary
explicit GC handles unreferenced CAS files later.

CLI, authenticated generation-guarded HTTP and native Qt use the same operation.
Real SDK loopback fixtures preserve their original three sends through recovery
and archive. Actual storage/codec faults cover accepted old response references,
unpublished review protection, missing metadata, malformed reference shapes,
continuation races and cancellation during metadata reads. The final diagnostic,
compiler, adapter, database, boundary and size matrix passes 79 tests. Mypy passes
592 modules. Focused Ruff, formatting and diff checks and both static readonly
review axes pass. Remaining obsolete roots, physical unregistered CAS orphans
and the sole P8 activation are still pending; issue 164 remains open.

### Physical CAS registration-gap recovery

Explicit GC now discovers canonical regular CAS files left after durable file
publication but before the Blob registry transaction committed. Preview and
collection count them separately while sharing the existing object/byte batch
budget. Unknown names, directories and symbolic links are retained. No automatic
scanner or background collector is introduced.

The directory scan reads names and stat metadata only, with a generation
lifecycle lease and no content/reclaim lease, ingest lock or SQL transaction.
It can traverse the whole CAS directory, so its total discovery time is not
bounded by candidate count; only the retained candidate batch is bounded.
Preview performs bounded verification under its existing read locks. Deletion
holds exclusive reclamation, checks pending receipt ownership and rechecks each
candidate's registration, canonical path, regular-file type and size. A new
registry row protects the file even when it currently has no business reference.
No SQL row is created merely to delete an unregistered name. An interrupted
durable unlink can be retried by a later explicit batch.

Real SQLite triggers first reproduce the missing-orphan behavior. Actual
directory-I/O delay then exposes a preview lease regression and verifies its
repair: both discovery paths permit independent exclusive reclamation and
ingest-lock acquisition while discovery is paused. Additional witnesses cover
readmission between discovery and exclusive acquisition, byte/count limits,
unresolved receipts, busy content access and all three actual maintenance
adapters. The final maintenance/conversation/tree/backup/usage/boundary/size
matrix passes 75 tests (5 live tests deselected). Mypy passes 593 modules; focused
Ruff, formatting and diff checks and both static readonly review axes pass.
Remaining obsolete-root and final P8 activation work continues.

### Abandoned unsealed generator plans

The explicit shared root-retirement operation now includes abandoned
`building` generator plans and resumes its own `retiring` plans. Any active
execution on their original model budget, already linked accepted/conflict
artifact revision, or unexpected owned reference protects the plan. Ready plans
remain retained recovery results. This does not expire accepted calculations,
provider receipts, core budgets or artifact history.

Selection shares the remaining object and encoded-byte budgets with session
checkpoints and sync plans. Encoding happens outside the SQL writer. Under
exclusive reclamation the operation rechecks eligibility, durably marks a plan
retiring, and removes each selected file row and its exact owned Blob reference
in one transaction. Only an empty retiring plan releases its details reference.
Every transaction has bounded row/parameter work; a later batch can continue
after a file-deletion failure. Physical CAS deletion remains a separate GC step.

A real SQL seal failure after two actual SDK loopback sends leaves a building
plan with two files. Bounded retirement preserves paid requests, original budget
and all accepted compiler steps. Actual generation resumes successfully with
the same two sends. Additional witnesses cover an active continuation budget,
insufficient byte budget, interrupted retirement, ready-plan protection and
CLI/authenticated generation-guarded HTTP/native Qt operations.

The final generator/root/maintenance/orphan/boundary/size matrix passes 57 tests;
mypy passes 594 modules. Focused Ruff, formatting and diff checks and both static
readonly review axes pass. Other obsolete knowledge/review candidates and the
sole P8 activation remain unfinished.

### Interrupted compiler revision preparation

Development schema 38 adds bounded retirement for abandoned `draft` and
`sealed` knowledge preparations. Complete `ready` candidates, publications,
receipts, import/refresh/lint review owners, generator results and active
projection plans remain protected. Actual preparation holds its storage-stage
lease until the ready result is returned, so exclusive maintenance cannot
retire an in-progress preparation.

The operation shares object and encoded-parameter budgets with all other root
types. It durably fences the revision as `retiring`, deletes page-input and
other concrete relations in foreign-key order, retires dependent sync plans
through their existing bounded protocol, then releases owned Blob/tree
references. The final revision and manifest reference are deleted together.
Every partial batch keeps foreign keys closed; physical collection between
batches cannot remove still-referenced payloads. Interrupted deletion resumes
without discarding accepted compiler work or provider accounting.

Real SQLite faults at the first file insertion and final ready transition
exercise both interruption points after two SDK loopback sends. Each bounded
retirement batch passes a foreign-key check and actual GC while the original
public version remains readable. Retrying compilation reuses all accepted
steps with the same two sends. Additional cases cover insufficient byte
budget, complete-candidate protection, interrupted child deletion and the CLI,
authenticated generation-guarded HTTP and native Qt operations.

The final preparation/generator/root/maintenance/diagnostic/database/boundary/
size matrix passes 61 tests. Mypy passes 595 modules; focused Ruff, formatting,
diff checks and both static readonly review axes pass. Complete obsolete review
candidates and the sole P8 activation remain pending; issue 164 is still open.

### Explicit transcripts from retained conversation originals

The shared export use case now accepts a development main-store scope and a
retained conversation identity. It takes the real session lock, reads bounded
original/pending pages from the authoritative store and publishes the complete
transcript through the knowledge protocol without resolving model credentials
or sending a request. Original histories, their versions and core costs remain
unchanged. Existing ghost-link, unique-name and name/title/first-user-question
fallback policies are preserved, including originals with a leading system
message. Deleted conversations cannot be revived by export.

Master publication success and worktree synchronization remain distinct. A
paused worktree still permits immediate master reads; stale publication scopes
retain a conflict candidate without claiming an exported file. The native
worker now carries the publication, pending synchronization and candidate facts
through its result. No public factory is activated by this batch.

The initial application test reproduces the unsupported export; an actual
worker test reproduces dropped publication/synchronization state. A 129-turn,
130-pending-question case verifies complete ordered output, exact multiplicity
and the first-user filename fallback. A leading system message first reproduced
the role-selection regression and verifies its repair. The final shared/native
session, worker, boundary and module-size matrix passes 63 tests. Mypy passes
596 modules; focused Ruff, formatting and diff checks and both static readonly
review axes pass after the naming fixes. Remaining lifecycle/performance and P8
gates still prevent overall issue closure.

### Input freezing and preflight before source admission

Actual import preparation now records separate durable freezing and preflight
stages after the top-level execution starts. Failed freezing keeps a temporary
input identity, name and available byte count without claiming a digest.
Preflight binds that same identity to the frozen content digest and size even
when content rejection intentionally admits no source. A supplied prepared
input still passes its current-input checks and records reuse. Rejection and
retain-without-text policies are unchanged; these stages add no model sends.

The temporary identity also follows the admission stage. Native task trees map
it to the admitted original using only the current bounded stage page, so
earlier stages stay grouped with their actual input. UI labels now describe
freezing, preflight and explicit transcript export in user terms.

An actual empty HTML import first reproduces the missing stages; missing input
and rejected frozen-content cases retain their real failure boundaries without
changing publication or budget. A regression first reproduces split temporary
identities, and a real Qt tree first reproduces duplicate pre/post-admission
input nodes. Both fixes pass the final 38-test import/task/native/boundary/size
matrix. Mypy passes 596 modules; focused Ruff, formatting, diff checks and both
static readonly review axes pass. Public activation remains gated by the
outstanding maintenance, equivalence and packaging acceptance.

### Offline settings isolation and Qt test lifetime

Validation exposed an existing gap: some offline task-adapter tests wrote real
application discovery/default settings, and the first legacy-reference runner
also registered its disposable libraries. The latter runner's three exact
registrations and temporary default were removed; unrelated configuration was
preserved. The previous intended default could not be established from the
remaining settings, so the user was asked to select it. This is not recorded as
having restored the previous default.

`ULTRAKB_CONFIG_DIR` now selects an explicit application settings directory at
process startup. Offline tests isolate both module paths and the inherited
worker environment; live tests retain their existing setup. Desktop default
task storage resolves the current configuration module path rather than an
import-time copy. Actual child-process registration verifies private writes
and unchanged bytes in the user's real global settings.

A combined regression run also reproduced an unrelated Qt test-lifetime fault:
a discarded five-second fallback timer closed windows from later tests. The
test now owns and stops both timers and scopes closing to its own library.
The same config/task/native/session/boundary/size matrix then passes 102 tests
(1 live test deselected); the separate native Workbench/configuration matrix
passes 4 tests. Mypy passes 596 modules. Focused Ruff, formatting, diff checks
and both static readonly review axes pass. Public activation is still pending.

### Explicit disposal of complete candidates

Schema 39 adds a small retained import-group disposal decision. Import
`original_id` is a validated opaque group identity rather than a foreign key
to a content-bearing candidate: deleting obsolete copies must not require
retaining an entire original knowledge revision. Published members remain
readable and replay their original success receipt; unpublished members of an
explicitly discarded group cannot reappear in review or become published.

The shared maintenance catalog lists complete knowledge and artifact-only
generator candidates in bounded pages. Disposal checks the selected manifest
version and database identity under nonblocking exclusive reclamation, including
the pending-receipt barrier. It refuses published history, current projection
ownership, accepted/conflict artifacts, and any running/stopping execution.
This last conservative restriction applies only to explicit candidate disposal;
ordinary orphan GC remains available during pure model waits.

Disposal fences the selected revision and removes its review roots atomically.
Other discarded import-group members and generator files/plans retire through
the existing bounded maintenance protocol before Blob/tree collection. Core
budgets, request receipts, originals, and accepted compiler/generator steps are
retained. CLI, authenticated HTTP, and Qt invoke the same shared operation.

Actual SDK/loopback scenarios verify import, refresh, lint and generator retry
after disposal without new sends, including changed projection plans and old
review copies. Active execution, busy reader, wrong generation, and published
history are protected. The final regression matrix passes 73 tests, Mypy passes
598 modules, focused Ruff/format/diff checks pass, and both static readonly
review axes report zero findings. Public factories are still unchanged.
