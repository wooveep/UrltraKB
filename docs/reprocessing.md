# Recompile, retry, and explicitly reprocess

Recompilation reads a retained unit's normalization and saved index. Its recorded
execution mode governs the operation, including short documents that used
segmented execution. It does not convert Office files, split worksheets, or
reclassify the document using today's thresholds.

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
availability, current and saved policies, worksheet scope, version implications,
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
Pending or cancelled version clarifications must be resolved before a new
request. Quality notes and unfinished compilation stages remain visible.

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
verified original, and waits for version clarification before compiling new
knowledge. Matching original and normalized bytes still create an `original`
revision instead of reusing a `legacy_snapshot`. Old mixed pages are not silently
assigned a product version or split into worksheets during library upgrades.
