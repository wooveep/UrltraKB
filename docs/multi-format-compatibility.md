# Multi-format compatibility and verification

Development issues #73–#99 share the source admission, unit publication,
knowledge view, evidence, and pending-work contracts. CLI, HTTP, desktop tasks,
and raw-directory watching use these application services.

## Source operations

| Operation | CLI | HTTP | Desktop |
| --- | --- | --- | --- |
| Read retained source / ranges | `source SOURCE_ID` | `/api/v1/document/source` | Read original; worksheet reader |
| Retry unfinished source units | `retry-source SOURCE_ID` | `/api/v1/document/retry` | Retry selected unfinished source |
| Retry a worksheet | `retry-worksheet SOURCE_ID --unit UNIT_ID` | `/api/v1/document/retry-worksheet` | Worksheet retry |
| Recompile saved processing | `recompile` | Recompile preview / execute | Recompile selected source / worksheet |
| Change processing policy | `reprocess SOURCE_ID`, then `--execute VERSION` | Reprocess preview / execute | Preview reprocessing, then confirm |
| Discover/import embedded files | `process-pending` | Pending process / retry | Pending work; idle dispatcher |
| Review versions / proposals | `versions`, `proposals` | Version review / proposal routes | Version clarification / pending differences |

Use the command's `--help` or the HTTP OpenAPI schema for range, view, and
revision parameters. `pages` means physical PDF pages, `blocks` frozen text
blocks, `chars` Unicode character positions, and `cells` worksheet coordinates.
Unknown legacy measurements remain unknown. A workbook is one source with
independently published worksheet units.

Retries use the current retained original and saved policy, skip successful
units, and keep older successful knowledge if a replacement fails. They do not
require the user's external file to remain at its former path. Missing frozen
inputs or incompatible saved processing block the action; explicit
[reprocessing](reprocessing.md) supplies a reviewed new processing revision.

An import's body status and its discovery/import counts are independent. A
failed workbook inventory can still have a pending discovery job. Quality
warnings and unfinished compilation stages are returned even when some
knowledge was published. CLI and desktop result text use the same projection;
HTTP JSON/SSE and watch events retain the same structured outcomes.

## Catalog writer compatibility

`.openkb/catalog/schema.json` records `schema_version`,
`minimum_writer_version`, and `required_capabilities`. Record schema 1 and
catalog writer protocol 2 are supported by this implementation. These numbers
describe storage protocols, not the application package version.

The authoritative capability list is `openkb.catalog_schema.SUPPORTED_CAPABILITIES`:
source revisions, unit publication, worksheet units/lifecycle, durable import
dispatch, knowledge views, version review, query views, knowledge refresh,
frozen normalization, multi-format sources, explicit reprocessing, and embedded
discovery v3. Native host policies are frozen per discovery intent; older
intents retain their earlier policies.

An unknown capability, unsupported schema, or higher minimum writer version
blocks writes before journal recovery or business changes. This applies to
ordinary synchronous/asynchronous mutations, cancellation signals, explicit
repair, and resumed deletion. A local worker can still be stopped without
writing an incompatible catalog. A compatible write updates the capability
marker under the KB lease; reading a legacy library does not migrate it or
create a marker. Reading committed supported content remains available; a
pending journal that needs an unsupported writer cannot be repaired on read.

Do not alternate this version and an older released binary against the same
new-format KB. Older binaries that predate the catalog guard cannot be made
safe by a marker they do not understand. Retain a separate backup for rollback
and use a matching program version for writing and repair.

Raw watchers observe user files under `raw`, excluding hidden directories and
dotfiles. Managed normalized sources, embedded payloads, history, and knowledge
outputs live outside the watched input tree. Discovery consumes frozen originals
through its durable queue; it is not a second raw-directory watcher.

## Deterministic checks and human acceptance

`tests/test_multi_format_integration.py` exercises real source state across
CLI, HTTP, spawned desktop workers, and the filesystem watcher. It covers
retained-input retries, read-only legacy access, measurements, quality signals,
discovery counts, compatibility guards, recovery, and cancellation. Related
format tests use small fixed files; only external model/network boundaries
are substituted. Office integration uses the prepared private runtime.

Run the complete development checks with the pinned environment:

```bash
uv sync --all-extras --offline
OPENKB_TEST_OFFICE_RUNTIME=/path/to/prepared-runtime .venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy openkb
```

These checks do not approve a release. Keep the following issues open until
their human operator has recorded the evidence and conclusion:

| Issue | Human verification still required |
| --- | --- |
| #100 | Selected real-model PDF/Markdown samples: model/config/input identity, facts and conditions, version/source accuracy, calls, timing and failures. |
| #101 | Final Windows 11 x64 archive on a clean standard-user machine: bundled Office/Python/UNO/fonts/CFB helper, native dependencies, offline and Chinese/space paths, read-only install, coexistence, cancellation, all format/host samples. |
| #102 | Final Debian 13.6 x64 GNOME/X11 archive: actual x86-64-v2 and ELF dependencies, private runtime, clean/offline/path/permissions/coexistence cases, timeout/cancellation cleanup, real font rendering, all format/host samples. |

For each human run retain the exact archive digest, source commit, runtime
manifest/build IDs, environment, sample digests, resulting source/unit/revision
IDs, diagnostics, screenshots or logs, and pass/fail conclusion. Automated
skips, fixture model output, or a development-machine run do not substitute for
these acceptance results. Report failures against the owning development issue,
repair the shared implementation, and repeat the affected human checks.
