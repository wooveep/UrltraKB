# REST API

This guide covers the independent OpenKB REST API (FastAPI). The native desktop opens local knowledge bases directly; the API does not serve a browser application at `/`.

> The interactive API reference is served live at [`/docs`](http://127.0.0.1:7566/docs) (OpenAPI/Swagger) once the server is running — you can import `/openapi.json` directly into Postman.

## REST API


OpenKB ships a FastAPI service for using a knowledge base from Postman,
frontends, or other HTTP clients.

Install the API dependencies if needed:

```bash
pip install -e ".[api]"
```

Start the API server:

```powershell
$env:OPENKB_API_TOKEN="test-token"
$env:OPENKB_KB_ROOT="D:\project\OpenKB\kbs"
.\.venv\Scripts\python.exe -m openkb.api --host 127.0.0.1 --port 7566
```

### Authentication and common behavior

Auth is **opt-in**, controlled by the `OPENKB_API_TOKEN` server environment
variable:

- **Unset (default)** — the API is unauthenticated. This is the local-first
  default for `openkb-api`.
- **Set** — every request must carry the token:

  ```text
  Authorization: Bearer <OPENKB_API_TOKEN>
  ```

  A missing or wrong token is rejected with `401`.

> **Exposing the server?** Always set `OPENKB_API_TOKEN` when binding to a
> non-loopback host (e.g. `--host 0.0.0.0`) — otherwise the API, and every KB
> it can reach, is world-open. `openkb-api` prints a warning in that case.

`OPENKB_KB_ROOT` is optional. It controls where REST-created knowledge bases
are stored. If unset, OpenKB uses `~/.config/openkb/kbs`.

REST clients identify a knowledge base with `kb`, not a filesystem path. For
example, `postman-kb` resolves to `$OPENKB_KB_ROOT/postman-kb`.

Common status codes across all endpoints:

- `200` — success.
- `400` — invalid request body, unknown `kb`, or a KB that isn't an OpenKB dir
  (missing `.openkb/` or `wiki/`).
- `401` — missing or wrong bearer token (only when `OPENKB_API_TOKEN` is set).
- `404` — referenced document/watcher not found (remove/recompile/watch-stop).
- `409` — ambiguous identifier (remove/recompile) with `candidates` in `detail`.
- `500` — server error.

All JSON endpoints use `Content-Type: application/json`. `/api/v1/add` is the
only `multipart/form-data` endpoint.

### Streaming (SSE)

Endpoints that accept `"stream": true` (`query`, `chat`, `remove`, `recompile`),
plus `add` (via a `stream` form field) and `watch/events` (always), return
Server-Sent Events (`Content-Type: text/event-stream`). Each frame is:

```text
event: <name>
data: <json-object>
```

SSE event names:

- `start` — stream opened; `data` includes the `endpoint`.
- `delta` — incremental answer text (query/chat), `{"text": "..."}`.
- `tool_call` — agent tool invocation (query/chat), with the call details.
- `uploaded` / `file_start` / `file_done` — per-file progress (add).
- `plan` — execution plan (remove: full plan; recompile: target list).
- `progress` — stage progress (remove: `wiki_cleanup`).
- `doc` — one document recompiled (recompile stream).
- `final` — terminal success payload (matches the non-streaming JSON body).
- `error` — failure, `{"message": "..."}` (may carry `code` for remove).
- `done` — stream closed; always emitted last.

### Endpoints

All endpoints are under `/api/v1`.

| Method | Path                    | Body         | Streams  | Purpose                                |
| ------ | ----------------------- | ------------ | -------- | -------------------------------------- |
| GET    | `/kbs`                  | —            | no       | List knowledge bases under the KB root |
| POST   | `/init`                 | JSON         | no       | Create a knowledge base                |
| POST   | `/add`                  | multipart    | optional | Upload + compile documents             |
| POST   | `/query`                | JSON         | yes      | Ask a one-shot question                |
| POST   | `/chat`                 | JSON         | yes      | Multi-turn chat session                |
| POST   | `/chat/sessions`        | JSON         | no       | List persisted chat sessions           |
| POST   | `/chat/sessions/load`   | JSON         | no       | Load a session's history               |
| POST   | `/chat/sessions/delete` | JSON         | no       | Delete a session                       |
| POST   | `/list`                 | JSON         | no       | List documents, summaries, concepts    |
| POST   | `/status`               | JSON         | no       | KB directory/index stats               |
| POST   | `/lint`                 | JSON         | no       | Structural + semantic lint report      |
| POST   | `/remove`               | JSON         | yes      | Remove a document + cleanup            |
| POST   | `/recompile`            | JSON         | yes      | Recompile one or all docs              |
| POST   | `/watch/start`          | JSON         | no       | Start a filesystem watcher             |
| POST   | `/watch/stop`           | JSON         | no       | Stop a watcher                         |
| POST   | `/watch/status`         | JSON         | no       | Watcher status + recent events         |
| GET    | `/watch/events`         | query params | always   | SSE feed of watcher events             |

#### Initialize a KB

```http
POST /api/v1/init
Content-Type: application/json
Authorization: Bearer test-token
```

Request (`InitRequest`):

| Field             | Type   | Required | Default | Notes                         |
| ----------------- | ------ | -------- | ------- | ----------------------------- |
| `kb`              | string | yes      | —       | new KB name                   |
| `model`           | string | no       | `null`  | LLM model override            |
| `api_key`         | string | no       | `null`  | written to KB-local `.env`    |
| `openai_api_base` | string | no       | `null`  | OpenAI-compatible gateway URL |

`api_key` and `openai_api_base` are written to the KB-local `.env` when the KB
is created; secret values are not echoed back in the response.

When `model`, `api_key`, and `openai_api_base` are all omitted, the new KB inherits the operator's project-root `config.yaml` and LLM
credentials from the server's working-directory `.env` (server-level `OPENKB_*`
vars are filtered out), so a newly created KB can use the server's existing model connection.

Response (`InitResponse`, `200`): `kb` (string), `created` (bool, `false` if the
KB already existed), `env_written` (`{api_key: bool, openai_api_base: bool}`),
`message` (string). Errors: `400` invalid/existing KB, `500` other.

#### Add Documents

```http
POST /api/v1/add
Authorization: Bearer test-token
```

`multipart/form-data`:

| Field    | Type | Value                 |
| -------- | ---- | --------------------- |
| `kb`     | Text | `postman-kb`          |
| `stream` | Text | `true` or `false`     |
| `files`  | File | one or more documents |

Supported types match the CLI: `.pdf`, `.md`, `.markdown`, `.docx`, `.pptx`,
`.xlsx`, `.xls`, `.html`, `.htm`, `.txt`, `.csv`. `stream: true` emits per-file
SSE events (`uploaded`, `file_start`, `file_done`, `final`); `stream: false`
returns one JSON body. `400` if no files are uploaded.

Response (`AddResponse`, `200`): `kb`, `files` (each
`{original_name, saved_path, status, message}`), `added_count`, `skipped_count`
(already indexed), `failed_count`.

#### Query

```http
POST /api/v1/query
Content-Type: application/json
Authorization: Bearer test-token
```

Request (`QueryRequest`):

| Field      | Type   | Required | Default | Notes                                |
| ---------- | ------ | -------- | ------- | ------------------------------------ |
| `kb`       | string | yes      | —       |                                      |
| `question` | string | yes      | —       |                                      |
| `stream`   | bool   | no       | `true`  | SSE vs single JSON                   |
| `save`     | bool   | no       | `false` | write answer to `wiki/explorations/` |

Non-streaming response (`QueryResponse`, `200`): `answer` (string), `saved_path`
(string\|null, set when `save: true`). Stream events: `start`, `delta`,
`tool_call`, `final` (`{answer, saved_path}`), `error`, `done`. `500` on failure.

#### Chat

```http
POST /api/v1/chat
Content-Type: application/json
Authorization: Bearer test-token
```

Request (`ChatRequest`):

| Field        | Type   | Required | Default | Notes                      |
| ------------ | ------ | -------- | ------- | -------------------------- |
| `kb`         | string | yes      | —       |                            |
| `message`    | string | yes      | —       |                            |
| `session_id` | string | no       | `null`  | resume an existing session |
| `stream`     | bool   | no       | `true`  | SSE vs single JSON         |

Non-streaming response (`ChatResponse`, `200`): `session_id` (pass back to
continue), `answer`, `turn_count` (total turns in the session). Stream events:
`start` (includes `session_id`), `delta`, `tool_call`, `final`, `error`, `done`.

#### Chat Sessions

List, load, or delete persisted multi-turn sessions for a KB. These sessions
use the same on-disk format as CLI and desktop conversations.

```http
POST /api/v1/chat/sessions
Authorization: Bearer test-token
```

Request (`KbRequest`): `{ "kb": "postman-kb" }`.
Response (`ChatSessionListResponse`): `{ "kb", "sessions": [{ id, title, turn_count, updated_at, model }] }`,
ordered by `updated_at` descending.

```http
POST /api/v1/chat/sessions/load
Authorization: Bearer test-token
```

Request (`ChatSessionLoadRequest`): `{ "kb", "session_id" }`.
Response (`ChatSessionLoadResponse`): `{ session_id, title, turn_count, user_turns: [...], assistant_texts: [...] }`.
Clients interleave `user_turns` and `assistant_texts` to render the history.
`404` if the session does not exist.

```http
POST /api/v1/chat/sessions/delete
Authorization: Bearer test-token
```

Request (`ChatSessionDeleteRequest`): `{ "kb", "session_id" }`.
Response (`ChatSessionDeleteResponse`): `{ deleted: true }`. Returns `404` if not found.

#### List

```http
POST /api/v1/list
Content-Type: application/json
Authorization: Bearer test-token
```

Request (`KbRequest`): `kb`.

Response (`ListResponse`, `200`): `documents`
(`[{hash, name, type, display_type, pages}]`), `document_count`, `summaries`,
`concepts`, `reports` (lists of page names).

#### Status

```http
POST /api/v1/status
Content-Type: application/json
Authorization: Bearer test-token
```

Request (`KbRequest`): `kb`.

Response (`StatusResponse`, `200`): `directories` (per-folder file counts),
`raw_count`, `total_indexed`, `last_compile` and `last_lint` (ISO timestamps or
`null`).

#### Lint

```http
POST /api/v1/lint
Content-Type: application/json
Authorization: Bearer test-token
```

Request (`LintRequest`):

| Field | Type   | Required | Default | Notes                                      |
| ----- | ------ | -------- | ------- | ------------------------------------------ |
| `kb`  | string | yes      | —       |                                            |
| `fix` | bool   | no       | `false` | rewrite/strip broken `[[wikilinks]]` first |

When `fix: true`, broken wikilinks are rewritten/stripped under the KB ingest
lock (mirroring `openkb lint --fix`) before the report runs, so the report
reflects the post-fix state. The semantic lint is a multi-turn LLM agent run, so
the response can take tens of seconds to minutes regardless of `fix`; the `fix`
pass itself is a local, millisecond file rewrite.

Response (`LintResponse`, `200`):

| Field                 | Type         | Notes                                       |
| --------------------- | ------------ | ------------------------------------------- |
| `skipped`             | bool         | `true` when no documents are indexed        |
| `reason`              | string\|null | e.g. `no_documents_indexed`                 |
| `message`             | string       | status + fix summary when `fix: true`       |
| `structural_report`   | string\|null | local structural lint markdown              |
| `knowledge_report`    | string\|null | LLM semantic lint markdown                  |
| `report_path`         | string\|null | report under `wiki/reports/`                |
| `lint_files_changed`  | int\|null    | files rewritten by `fix` (else `null`)      |
| `lint_ghosts_removed` | int\|null    | ghost links stripped by `fix` (else `null`) |

#### Remove Documents

Remove a document and clean up its wiki pages, images, registry, and PageIndex
state, the same pipeline as `openkb remove`.

```http
POST /api/v1/remove
Content-Type: application/json
Authorization: Bearer test-token
```

Request (`RemoveRequest`):

| Field        | Type   | Required | Default | Notes                                   |
| ------------ | ------ | -------- | ------- | --------------------------------------- |
| `kb`         | string | yes      | —       |                                         |
| `identifier` | string | yes      | —       | filename, `doc_name` slug, or substring |
| `keep_raw`   | bool   | no       | `false` | keep the source file                    |
| `keep_empty` | bool   | no       | `false` | keep now-empty concept/entity pages     |
| `dry_run`    | bool   | no       | `false` | preview only                            |
| `stream`     | bool   | no       | `false` | SSE vs single JSON                      |

Response (`RemoveResponse`, `200`): `status` (`removed`, `partial`, `dry_run`),
`name`, `doc_name`, `actions` (each `{tag, target}`), `concepts_deleted`,
`entities_deleted`, `lint_files_changed` and `lint_ghosts_removed` (scoped
`lint --fix` counts after removal), `pageindex_message`/`pageindex_error`,
`message`, `candidates`. Errors: `404` no match, `409` multiple matches (with
`candidates`). Stream events: `start`, `plan`, `progress`, `final`, `error`,
`done`.

#### Recompile

Re-compile one or all documents, mirroring `openkb recompile`.

```http
POST /api/v1/recompile
Content-Type: application/json
Authorization: Bearer test-token
```

Request (`RecompileRequest`):

| Field            | Type   | Required | Default | Notes                             |
| ---------------- | ------ | -------- | ------- | --------------------------------- |
| `kb`             | string | yes      | —       |                                   |
| `doc_name`       | string | no       | `null`  | one doc; omit with `all_docs`     |
| `all_docs`       | bool   | no       | `false` | recompile every document          |
| `dry_run`        | bool   | no       | `false` | preview only                      |
| `refresh_schema` | bool   | no       | `false` | re-extract PageIndex schema first |
| `stream`         | bool   | no       | `false` | SSE vs single JSON                |

Response (`RecompileResponse`, `200`): `status` (`done`), `total`, `recompiled`,
`skipped`, `docs` (each `{name, doc_name, type, status, elapsed, message}`),
`targets`/`candidates` (present for plans/ambiguity). Errors: `404` no match,
`409` ambiguous (with `candidates`), `500` otherwise. Stream events: `start`,
`plan` (`{targets}`), `doc` (per-document result), `final`, `error`, `done`.

#### Watch (auto-compile on file change)

Start/stop/inspect a filesystem watcher that auto-compiles files dropped into
`raw/` (same as `openkb watch`), plus an SSE feed of watcher events.

```http
POST /api/v1/watch/start
POST /api/v1/watch/stop
POST /api/v1/watch/status
GET  /api/v1/watch/events
Content-Type: application/json
Authorization: Bearer test-token
```

`watch/start` (`WatchStartRequest`): `kb`, `debounce` (seconds, default `2.0`,
must be `> 0`). `watch/stop` and `watch/status` take only `kb`.

```json
{ "kb": "postman-kb", "debounce": 2.0 }
```

All three return `WatchStatusResponse`: `kb`, `active` (bool), `started_at`
(epoch or `null`), `raw_dir`, `debounce`, `counters` (`{added, updated, failed,
...}`), `recent_events` (`[{ts, event, data}]`). `watch/stop` returns `404` if
no watcher is active for that KB.

`GET /api/v1/watch/events` is always SSE. Query params: `kb` (required),
`max_events` (int, `>=1`, stop after N events), `timeout_seconds` (float, `>=0`,
stop after this many seconds). Stream events: `start`, the watcher's own events
(e.g. `added`, `updated`, `failed`, `final`), `error`, `done`.

The full OpenAPI schema is at [`/openapi.json`](http://127.0.0.1:7566/openapi.json) — importable into Postman or any OpenAPI-compatible client.
