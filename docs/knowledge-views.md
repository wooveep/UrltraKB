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
source's confirmed labels. Importing never copies another view's knowledge.

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
