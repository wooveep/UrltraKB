# Knowledge views

A knowledge view contains one product and its complete applicability scope.
Installation and operations manuals for the same product and the same complete
set of versions share a view. Overlapping version ranges, different products,
and sources with unknown applicability stay separate. A manual's own revision
label (for example `R2`) is separate from the product versions it applies to.

For PDF imports, supply confirmed metadata when available:

```bash
openkb add install.pdf --product WinStack --applicable-version 9.4 --family installation --document-revision R2
openkb views list
openkb --view <view-id> list
openkb --view <view-id> query "How do I install this version?"
openkb --view <view-id> recompile install.pdf
```

Repeat `--applicable-version` for a complete set of supported versions. Missing
product or applicability creates a separate unknown view for that source.
Choosing an existing view explicitly supplies its product and applicability;
conflicting metadata is rejected. Reimporting without metadata preserves the
source's confirmed labels for the same input. Changed bytes need fresh
applicability evidence. Importing never copies another view's knowledge.

Reliable PDF titles supply candidates with their locations. Product versions
and document revisions remain separate; user corrections take precedence over
title candidates. The first source with unknown applicability imports normally.
A later related manual with missing or conflicting applicability waits for
clarification. Its original and conversion are retained, and its worker exits.

```bash
openkb versions list
openkb versions show <review-id>
openkb versions supplement <review-id> --metadata '{"applicable_versions":["9.4"]}'
openkb versions resume <review-id>
openkb versions cancel <review-id>
openkb versions open <source-id>
```

Supplement and resume accept multiple review IDs. Omitted metadata fields stay
unchanged. Supplementing saves a metadata revision; `resume` explicitly starts
another attempt using retained conversion without another upload. Cancelled
clarifications do not resume automatically. Replaced inputs cease to appear as
pending. `open` starts correction of an already imported source: resuming
compiles its retained input into the confirmed view, preserving the old view
and historical citations. It never relabels or copies mixed knowledge pages.

Desktop **资料管理** offers **查看版本待补** and **补充所选资料版本** with evidence,
batch editing, cancellation and explicit continuation. HTTP clients use
`GET /api/v1/version-reviews`, `POST /api/v1/version-review` (read),
`POST /api/v1/version-review/open` (`source_id`),
`POST /api/v1/version-reviews/supplement` (`updates` maps review IDs to metadata),
and `POST /api/v1/version-review/cancel` or `/resume` (`review_id`). All accept
the selected `view_id`; no action waits indefinitely for human input.

The desktop's **知识视图** selector applies to documents, knowledge pages,
conversations, maintenance and generation. The Documents form accepts product,
applicable versions, family and revision for file, directory and URL imports.
Each queued operation retains the view selected when it was submitted.
Conversations and page drafts belong to their own view.

HTTP clients list views with `GET /api/v1/views?kb=<name>`. Supply `view_id` in
JSON requests or as a multipart field for `/api/v1/add`. The upload's `metadata`
field accepts a JSON object with `product`, `applicable_versions` (an array),
`family` and `document_revision`. Import results include each unit's `view_id`.
Page, source, query, chat, lint and proposal operations use that same scope.

Existing mixed knowledge remains in `legacy`. Run `openkb views map-legacy`
(desktop: **映射旧资料**, HTTP: `POST /api/v1/views/map-legacy?kb=<name>`) to record
retained source evidence without invoking a model. This preserves current
content as `legacy_snapshot`; it does not infer missing originals, product
versions or historical knowledge. Missing retained evidence is reported.

Manual page edits create immutable snapshots. A compilation that would replace
manual changes saves a proposal for review. Use the selected view with
`proposals list`, `proposals show <id>`, and
`proposals accept <id> --version <reviewed-version>` to review and accept it.
