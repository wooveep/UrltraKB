# Document execution

Imports use one isolated task runtime in the CLI, REST API and desktop. A task
captures settings before document processing begins and keeps that configuration
for the rest of its batch. Updating settings applies to a new task.

## Results and stopping

Each document reports `source_intake`, `knowledge_compilation`, `stage`, `reason`,
`quality`, `unfinished`, `resources`, `warnings` and `usage`. A completed or
previously completed identical document is successful. Invalid plans, partial
model output and exhausted budgets cannot publish knowledge. An ordinary failed
or unfinished document allows the batch to continue; stopping ends the current
item and remaining items. Already committed documents remain committed.

`openkb add` exits with 0 for a successful batch, 1 for failed or unfinished work,
and 130 after a user interrupt. Parameter errors retain Click's validation
behavior. Ctrl+C requests stopping and waits for worker exit and recovery.

The REST `POST /api/v1/add` accepts an optional 32-character lowercase hexadecimal
`task_id` form field. Generate it before submitting if querying before the result
is needed. The ID is bound to the knowledge base and uploaded names and bytes:
repeating the same submission observes that task, and conflicting input returns
409. JSON results and SSE `result` events use the same result structure. The first
SSE `start` event includes the ID; `progress` events precede `result` and `done`.

The API uses one process per task-history directory; a second API worker fails
at startup instead of accepting tasks it cannot coordinate.

`GET /api/v1/tasks/{task_id}` reads persisted execution status.
`POST /api/v1/tasks/{task_id}/stop` requests stopping. Observe until the task is
terminal and `processes_reaped` is true; `stop_confirmed` is only true for a
confirmed stopped task. Ending a connection ends observation and leaves the task
running within its budget. Uploaded input remains owned until execution ends.
An interrupted process with an unconfirmed result is reported explicitly and is
never automatically replayed from its summary. A confirmed business receipt
survives failed auxiliary teardown; cleanup warnings are separate from knowledge
completion.

## Required execution profile

Set a `processing` mapping in `.openkb/config.yaml` or the global configuration.
No production budgets have been calibrated for this implementation yet; missing
limits produce `execution_budget_required` before a model request. A custom model
must also supply its verified context and output capacities; unavailable values
produce `model_capabilities_required`. These are configuration outcomes, not
successful imports. Use the same measured profile when comparing documents.

| Field | Meaning |
| --- | --- |
| `context_tokens` | Verified maximum total context for the selected model |
| `output_tokens` | Positive output reserve, less than context capacity |
| `request_timeout` | Finite positive seconds for a request without an explicit timeout |
| `stage_timeout` | Finite positive seconds allowed for one processing stage |
| `document_timeout` | Finite positive seconds for the whole document operation |
| `cleanup_timeout` | Finite positive seconds for an auxiliary cleanup operation or shutdown grace |
| `max_attempts` | Positive maximum attempts for one logical request |
| `max_requests` | Positive maximum observable model attempts across the document |
| `max_tokens` | Positive document token budget, including outstanding reservations |
| `concurrency` | Positive maximum number of concurrent model calls |

A valid explicit per-request timeout retains existing configuration precedence;
remaining stage and document time always bound it. The complete request includes
system instructions, document content, prior knowledge, tools, schema and the
output reserve. Requests reserve their input estimate and maximum output before
sending. Available usage settles that reservation; missing usage remains charged
at the reserved amount and is labeled unknown. Cached-token details do not imply
free usage. No prompt is silently truncated and no alternate model is selected.
SDK retry settings are disabled at the request boundary. The execution controller
retries only transient transport/service errors within the configured limits.
Observable attempts and unknown internal transport attempts are distinct fields.

Every task retains its own usage observations in its result history. An explicit
retry starts a new task and budget; keep the earlier task history when reviewing
cumulative consumption. This first batch does not provide persistent parse or
partial compilation checkpoints. On failed compilation its existing transaction
rolls back new raw and Wiki artifacts together. Durable independent source intake
and selective continuation are later delivery gates in issue #13.

## Maintained PageIndex distribution

This source tree includes a patched `pageindex==0.3.0.dev3+openkb.1` wheel under
`packaging/pageindex/`. Its manifest records the upstream release, commit, hashes
and MIT license. `openkb.patch` records the source changes; the upstream wheel and
its notices remain included. Rebuild with:

```sh
python scripts/build_pageindex.py
uv sync --frozen --python 3.12.13 --extra dev --extra api
```

For pip source installs, make the accompanying wheel available in the same command:

```sh
pip install --find-links packaging/pageindex -e ".[dev,api]"
```

Ship this wheel and manifest with any application wheel distribution. The local
version is not assumed to exist on PyPI. Source and desktop packaging include the
maintained sources and locked artifact. The patch fixes synchronous waits inside
PDF TOC processing, preserves cancellation through gathered results, exposes
caller-owned request execution, and closes model resources on the index's event
loop. It is rebuilt from checked-in upstream bytes, never by editing an installed
SDK in place.

## First-batch verification record

On 2026-09-09, CPython 3.12.13 was used on Debian 13 x64 and a Windows 11 x64
machine (build 26100.9445). These are synthetic model/control-flow results,
not OCR or semantic accuracy measurements:

- Debian compiler, application, REST, recompilation, diagnostic, desktop model
  and module-size regression selection: 352 passed.
- Debian review-fix, import-task, PDF fallback and converter selection: 48 passed.
- Debian auxiliary-warning and import-task selection: 20 passed, including
  actual slow-drip HTTP timeouts, POSIX CLI interrupt, forced teardown recovery,
  REST observation disconnect, historical reconnect and corrupt result handling.
- Windows shared-document, import-task, cancellation and PageIndex selection:
  54 passed, 1 POSIX signal test skipped.
- URL acquisition shares the document lease and elapsed budget with compilation;
  download/URL and task regressions passed on Debian (23) and Windows (22, with
  1 POSIX signal test skipped). Auxiliary/task checks also passed on Windows
  (18, with 2 POSIX-only tests skipped).
- Ruff and mypy passed (167 application modules). The application wheel and sdist
  built successfully; the sdist includes the patched SDK and processing guide.
  Rebuilding the PageIndex wheel on Windows and Debian produced identical bytes
  and the manifest's SHA-256, including under Windows Git newline conversion.
- Standards and Spec reviews reported no remaining findings after fixes.

Real model calibration, representative long-document fact/evidence measurements,
local PaddleOCR CPU/offline verification and PaddleOCR jobs validation are still
required. No production budget, OCR throughput or content-coverage result is
claimed by these control-flow tests. The full suite is reserved for the final
three-batch integration check.
