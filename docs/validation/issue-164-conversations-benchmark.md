# Issue 164: long conversation checkpoint and projection calibration

Measured 2026-10-19 with Python 3.12.13 / SQLite 3.53.1 on Linux x86_64.
Reproduce from a source checkout:

```bash
.venv/bin/python scripts/verify_storage_conversations.py --output /absolute/owned/output.json
```

All three cases use the actual application execution scope, captured configuration,
ConversationMemory, SessionStore, ChatIndex, ConDB and SQLite/CAS. The existing
repository HTTP provider fixture returns deterministic legal topic/classification/
split responses to the real SDK. There are no external model calls or paid
provider usage. No SDK or application method is replaced. Core requests, budgets,
responses and checkpoints remain enabled. Fixtures contain public generated text.

The 100 exchanges contain both full user and assistant text. Assertions verify
the restored turns equal their authoritative originals, full SDK round-trip
equality, identical final snapshot/retrieval digests across all three cases,
user-only analysis/retrieval, and no new sends for an already complete prefix.
Each accepted turn has its own durable checkpoint; every successful catch-up
invocation publishes at most one final projection.

## Workloads and timing

| Measurement | 100-turn backlog | Continuous 1 turn | 97-turn prefix + final batch of 3 |
| --- | ---: | ---: | ---: |
| Catch-up invocations / projections | 23 | 100 | 98 |
| Invocations limited by stage budget | 22 | 0 | 0 |
| Actual SDK loopback sends | 133 | 132 | 132 |
| Profiled workload wall seconds | 23.369 | 29.563 | 29.133 |
| Main-thread CPU seconds | 21.355 | 26.824 | 26.668 |
| SDK snapshot calls / cumulative wall seconds | 322 / 0.775 | 300 / 0.723 | 300 / 0.878 |
| SDK restore calls / cumulative wall seconds | 366 / 1.184 | 399 / 1.299 | 397 / 1.145 |
| SDK retrieval exports | 23 | 100 | 98 |

The existing six-send question-phase allowance remains unchanged. The backlog
case renews that allowance between explicit continuation invocations while
retaining its logical task budget; it does not claim one projection for an
unlimited 100-turn operation. Budget interruption can retry unfinished work: this
backlog case has one extra send and is not model-send-equivalent to continuous
processing. Every continuation's actual sends and diagnostics appear in JSON.

The controlled third case adds three turns to an already accepted 97-turn prefix
and completes them in one invocation with four sends and one projection. Its
whole-session send count and final SDK/retrieval state equal the continuous case.
It demonstrates bounded single-call batching on a long prefix without removing
the runtime budget limit. It is not a claim that a 100-turn backlog fits that limit.

Default cProfile timers measure cumulative wall time, not CPU. Separately, 20
unprofiled pure SDK snapshot/restore calls on each final prefix use `thread_time`:
snapshot medians are 1.343–1.361 ms, restore medians 1.419–1.442 ms; maxima are
1.789 / 2.107 ms. The first exploratory custom-thread-time profiler produced
inconsistent timing and was discarded. Workload wall figures include profiling,
turn acceptance and storage work, exclude initial configuration and final
inspection, and are not production throughput or a legacy speedup comparison.
This is one host/run; it was not hardware-isolated.

## Retained payload and pending retirement

| Measurement | Backlog | Continuous | Final batch of 3 |
| --- | ---: | ---: | ---: |
| Unique original turn payload bytes | 184,560 | 184,560 | 184,560 |
| Accepted checkpoints | 100 | 100 | 100 |
| Total encoded checkpoint bytes | 1,470,499 | 1,470,499 | 1,470,499 |
| Retained current checkpoint bytes | 28,693 | 28,693 | 28,693 |
| Unbound checkpoint bytes / count | 1,441,806 / 99 | 1,441,806 / 99 | 1,441,806 / 99 |
| All projection reference bytes | 3,245,767 | 13,118,062 | 12,583,800 |
| Retained projection reference bytes | 271,812 | 271,812 | 271,812 |
| Unbound projection reference bytes | 2,973,955 | 12,846,250 | 12,311,988 |
| Collectable older trees | 22 | 99 | 97 |

Every final full SDK snapshot is 211,899 bytes; its tree state without inline
turns is 28,429 bytes. Authoritative full originals still occupy their separately
retained payloads. Reference byte sums may overlap and exclude SQL tree rows,
database/WAL allocation, requests and receipts; they are not physical reclaimed
space. This calibration previews retirement without deleting retained history.

Continuous processing still serializes/restores growing SDK state, validates
prefixes and builds full projections. The measurements show explicit batch
projection reduction, not linear long-session complexity. These initial cases
alone did not establish legacy model-send parity; the follow-up below compares
the same invocation schedules. Neither measurement completes the entire issue
or public P8 activation.

The focused memory/session/root-retirement/conversation/write/boundary/size
regression matrix passes 62 tests. Ruff, formatting and diff checks and both
static readonly review axes pass.

## Follow-up: equivalent legacy invocation schedules

Measured 2026-10-19T16:05:58Z using the actual pre-activation legacy
`context.conversation.ConversationMemory` at `a56b0f59`, real `ChatSession`
original records, configuration capture, executor, SDK, ConDB and local HTTP
provider. The main-store reference is the JSON above from `9520f731`.
The same 100 fixed turn identities, original text, deterministic response rules
and six-send per-invocation allowance are used. Inputs are constructed as real
domain records and committed through each actual store; no memory/SDK/model
method is replaced. Pre-existing compiler/config-capture fixes were present in
the working tree; the compared memory and SDK implementations match their refs.

| Invocation schedule | Legacy sends | Main-store sends | Accepted prefixes and sends per call |
| --- | ---: | ---: | --- |
| Full backlog with bounded continuations | 133 | 133 | Identical |
| Continuous one-turn additions | 132 | 132 | Identical |
| Accepted 97-turn prefix plus final three | 132 | 132 | Identical |

All three legacy final SDK snapshots and user-only retrieval exports match the
main-store digests exactly. The archived runner's turn-identity normalization
is an identity operation for these fixed fixture IDs. It changes no SDK field
or content. Complete-prefix retries send nothing in either path. The 133 versus
132 difference therefore comes from different invocation schedules at the
existing stage limit; it is not an increase caused by the persistence change.
This establishes parity for this deterministic fixture, not every possible
model response or semantic retrieval workload. Legacy wall times were captured
only as run context and are not compared as a speedup: runs/profiling differ.

The successful reference run used isolated settings. The initial exploratory
runner registered disposable libraries in real settings; those three exact
entries and its temporary default were removed. Prior default restoration is
not claimed. Offline process isolation and its regression are documented in
the storage protocol record.

The frozen [reference runner](issue-164-conversations-reference.py) requires a
source checkout of `a56b0f59` and an existing private output directory:

```bash
python /absolute/current/docs/validation/issue-164-conversations-reference.py \
  /absolute/reference-checkout /absolute/owned/output-directory
```

It is historical validation source, not a legacy runtime or migration entry
point. [Reference results](issue-164-conversations-reference.json) retain every
invocation's accepted count and sends; the final assertions require parity for
all schedules, SDK state and retrieval state. This resolves the identified
conversation parity gap while the other P7/P8 gates remain outstanding.
