# Recompile, retry, and explicitly reprocess

Recompilation reads a retained unit's normalization and saved index. Its recorded
execution mode governs the operation, including short documents that used
segmented execution. It does not convert Office files, split worksheets, or
reclassify the document using today's thresholds.
Validation uses the selected saved normalization, source map, assets and conversion
provenance, including retained slide body/notes. It does not run original-format
text preflight, consult today's Office/CNKI runtime, or reread the original workbook.
Missing or damaged saved inputs/indexes fail without converting or rebuilding them;
the previous successful knowledge remains available.

Source-role qualification is shared by new index inputs, compilation and question
evidence. New block inputs record `source-structure-v1` in their processing policy.
Older retained inputs keep their exact bytes, fingerprint, units and recorded
parser policy, including cache recovery. Reading rechecks source-role annotations
against the complete retained source without rewriting its index. Missing format
declarations remain unconfirmed; opening a library never upgrades them implicitly.
New native PDF and Office inputs record `native-page-roles-v1`: table regions and
generated image occurrences have Unicode coordinates which remain aligned when
managed assets are relocated. PPT body/notes use the same native PDF body as the
plain PDF adapter. Both previous PDF and slide parser policies remain registered
for saved-cache recovery, without model calls or rebuilding navigation.
Old pages without table proof keep their text without certifying literal HTML;
old image inventories remain available at page granularity, with unconfirmed
excerpt positions. The previous HTML normalization policy remains readable;
new HTML normalization retains native table markup instead of flattening it.
Bare legacy text cannot certify a literal image reference or trigger reprocessing
from a printed data URI. Existing figure records and gallery assets remain readable;
current projections never overwrite historical figure records. Recorded generated
media ranges cannot anchor original slide navigation.

Ordinary import retries keep the target's processing policy and completed units.
If a missing normalization, missing workbook inventory, or changed index policy
would require different processing, the retry is blocked with an explicit
reprocessing explanation.

## Review a new processing request

```bash
openkb --kb-dir /path/to/kb reprocess SOURCE_ID
openkb --kb-dir /path/to/kb reprocess SOURCE_ID --execute PREVIEW_VERSION
```

The first command only reads the retained original and assets, runtime file
availability, current and saved policies, worksheet scope, publication implications,
pending proposals, and extraction budgets. It performs no conversion or model
calls. Office startup is checked during execution, after the runtime file check.
The desktop **预览重新处理** action displays this same plan. HTTP clients use
`POST /api/v1/document/reprocess/preview` with `kb`, `source_id` and optional
`view_id`, then `POST /api/v1/document/reprocess` with the returned `version`.

Execution validates the plan inside the actual execution configuration snapshot
and the knowledge-base write lease. A changed plan must be reviewed again. The
request creates a new source/processing revision envelope over the same original
and asset blobs, recording `reprocessed_from`, the request token, and its policy.
It does not change the source identity or claim that the original bytes changed.
Request IDs and timestamps do not enter content/index policy fingerprints.

Repeating a submitted token resumes only its current admitted target. Failed or
interrupted work keeps the same source and unit revisions; successful worksheets
are skipped. A completed request returns `skipped`. Once another input or request
supersedes it, the old token is rejected. A fresh preview is required to request
another processing revision, even when the policy is unchanged.

Each workbook reprocess reads a complete inventory using the confirmed reader
policy. Failed sheets retain their previous successful knowledge. Manual edits
still produce proposals; a new target makes older pending proposals stale.
Quality notes and unfinished compilation stages remain visible.

Supported container formats create a separate durable extraction group with the
reviewed budget and discovery policy. Existing groups and recovered sources
remain independent. Repeating the same request does not create another group.
Body failure or stopping does not implicitly cancel extraction work.

## Legacy knowledge bases

Opening or previewing a legacy library does not map or reprocess its contents.
A legacy reprocessing preview requires an actual registered original whose bytes
match its recorded digest. A normalized Markdown/JSON snapshot alone is not an
original. Missing originals, assets, or required capabilities block the action.

Explicit execution captures the old mixed knowledge as history, admits the
verified original, and compiles it into the shared knowledge base. Matching original and normalized bytes still create an `original`
revision instead of reusing a `legacy_snapshot`. Existing pages remain in their captured history; new worksheet units come from
the explicit processing request.
