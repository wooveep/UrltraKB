# Refreshing knowledge after source changes

Updating a source, withdrawing it with `remove`, or explicitly confirming an empty
contribution records dependent pages as `needs_refresh` in the same transaction.
Marking does not call a model. Unchanged pages keep the source revisions that
actually produced them. Their text and frozen originals remain readable.

Current questions and subsequent compilations exclude pending pages and inactive
inputs. A historical read identifies the actual knowledge and source revisions.
Importing a different product applicability leaves the earlier view independent.
An explicit family default remains saved even when its view has no current evidence.
Empty and withdrawn inputs remain excluded even after a later import of the same
source into another view.

Select a view from `openkb views list`, then use:

```sh
openkb --view VIEW refresh status
openkb --view VIEW refresh run
openkb --view VIEW refresh proposals
openkb --view VIEW refresh show PROPOSAL
openkb --view VIEW refresh accept PROPOSAL --version REVIEWED_VERSION
openkb --view VIEW refresh history
openkb --view VIEW refresh read-history KNOWLEDGE_REVISION summaries/PAGE
```

Refresh rebuilds generated knowledge from that view's successful, currently valid
retained inputs. It does not parse the originals again. Finish a failed or pending
source import before refreshing. Withdrawn or explicitly empty contributions are
excluded. If no input remains, generated current pages retire while snapshots stay.
Failure preserves the old head and refresh reasons. A change to inputs, pages, or
the KB generation prevents a late candidate from publishing. Manual changes produce
a durable difference for review, including when refresh would delete a page.
Legacy registry entries without published normalized inputs block refresh rather
than being treated as empty evidence. Complete their source mapping and
recompilation before refreshing. Withdrawal of a mapped legacy source conservatively
marks uncertain old pages and preserves their bodies, including on repeated removal.

`openkb refresh empty SOURCE --generation GENERATION` explicitly confirms that the
reviewed source revision contributes no current knowledge. `openkb list` shows its
generation. The operation rejects a changed source. Import and recompilation do not
silently clear remaining refresh reasons.

In the desktop Documents panel, **待刷新知识** opens the view status, refresh action,
saved differences and **阅读历史知识**. **确认所选资料贡献为空** binds confirmation to
the inspected revision. Reading a pending page displays its validity and actual
source revisions in the source panel.

HTTP clients use `GET /api/v1/refresh/status`, `POST /api/v1/refresh`,
`POST /api/v1/refresh/empty`, `GET /api/v1/refresh/proposals`,
`POST /api/v1/refresh/proposal`, `POST /api/v1/refresh/accept`, and
`GET /api/v1/refresh/history`. Requests select `kb` and `view_id`; empty confirmation
also supplies `source_id` and `generation`, and acceptance supplies `proposal_id`
and the reviewed `version`. `POST /api/v1/page` accepts `knowledge_revision_id` for
an immutable historical read. Page responses include validity and actual revisions.

Removal retains referenced originals, assets, indexes, normalization caches, saved
differences and persistent import intents. `openkb refresh references` (or
`GET /api/v1/refresh/references`) reports the conservative inventory. Explicit
`openkb refresh cleanup PATH...` only removes managed original/cache artifacts
proven unreferenced; incomplete ownership records defer cleanup. Removing a source
does not remove independent sources recovered from it.
