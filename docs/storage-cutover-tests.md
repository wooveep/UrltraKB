# Native storage test cutover

Issue #164 replaces the file persistence protocols. Tests must create a native
SQLite/CAS store and explicitly publish their fixture pages. A working-copy edit
remains an unpublished edit; fixtures never synchronize it implicitly into the
main store. SDK integration uses the real SDK with loopback HTTP services.

## Retired implementation assertions

| Former test module | Retained requirements and native evidence |
| --- | --- |
| `test_state.py` | Hashing remains in `test_file_digest.py`. Source identity, same-byte deduplication, retained originals and independent processing versions are covered by `test_storage_catalog.py`. Hash-keyed registry CRUD and legacy stem backfilling have no native equivalent: content hashes do not identify sources. |
| `test_chat_session.py` | `test_storage_sessions.py`, `test_storage_session_management.py` and `test_storage_conversations.py` verify original messages, traces, restart, stale submissions, deletion and independent turn commits. Pure trace completion cases remain in `test_chat_trace.py`; pixel serialization remains checked by `test_image_delivery.py`. JSON session files and migration from missing JSON fields are retired. Cancellation and SDK settlement use `test_storage_model_execution.py` and `test_storage_conversations.py`; their contract permits GC during model waits and retains the session/lifecycle protection. |
| `test_usage_incremental_projection.py` | `test_storage_usage.py`, `test_storage_model_execution.py` and `test_storage_usage_audit.py` cover late/duplicate/partial receipts, response retention when SQL fails, indexed counters, and bounded audit/rebuild without deleting paid evidence. The JSON ledger and its second SQLite projection are removed. |
| File-based cases in `test_compile_checkpoints.py` | Native Blob/reference fault cases, changed-condition/page-base guards and repair limits remain in that module. `test_storage_compiler.py` and `test_storage_compiler_outcomes.py` use actual SDK requests to cover retained summary/plan recovery and the original budget after cancellation or publication failure. Migration from older compiler/figure protocols is outside the v2 format contract. |
| File factory cases in `test_indexer.py` / `test_index_checkpoint.py` | Configuration forwarding and protected-manual-page algorithms remain. `test_storage_indexing.py` covers actual SDK builds, full native coordinates, receipt recovery, and index reuse after publication failure; `test_storage_publications.py` covers sharing a ready document index across publications. Per-source SQLite copies, JSON checkpoint markers, and their WAL byte checks are retired; active master WAL/SHM validation is in `test_storage_database.py`. |
| `test_index_recovery_compatibility.py` | The assertion pins a pre-cutover SDK identity specifically to recover old normalization directories. V2 has explicit processing and index fingerprints and refuses old KBs. Native policy/index reuse remains covered by `test_storage_reprocessing.py` and `test_storage_indexing.py`. |
| JSON-specific cases in `test_application_sessions.py` / `test_chat_usage.py` | Versioned deletion, latest-history export, token normalization, and SDK usage assertions remain. Native session management/export and model-execution tests cover cancellation and duplicate receipts. Editing JSON session identities, newline variants and old SDK tool-history migration are retired. |
| Converter registry and lint registry classes | Converter tests retain parser/Office I/O seams and now require a private workspace. Admission/deduplication belongs to `test_storage_catalog.py` and native import tests. Lint tests explicitly publish fixture changes; native lint/repair cases check committed pages and navigation. |
| `test_compile_recovery_boundaries.py` / `test_compile_recovery_execution.py` | Their hand-authored JSON tasks, ledgers and interrupted-repair markers are retired. `test_storage_compiler.py`, `test_storage_compiler_outcomes.py`, `test_storage_model_execution.py`, `test_storage_usage.py` and native `test_compile_checkpoints.py` cover real received responses, accepted plans, bounded repair, unknown-send refusal, independent usage, cancellation and cumulative budgets. The v2 protocol never treats an unconfirmed charged request as permission for another send. |
| `test_conversation_context.py` | `test_storage_sessions.py` and `test_storage_memory.py` cover exact original-message codec round trips, user-only analysis, accepted-prefix recovery, failed final projection, cancellation and skipped recall. JSON projection poisoning/migration has no native reader. |
| `test_conversation_writes.py` | `test_storage_conversation_writes.py` covers real SDK tool publications, persisted stop, competing heads, independent retained notes and answer recovery without resend. `test_storage_generators.py` and `test_storage_skill_maintenance.py` cover versioned artifacts, failed edits, history and rollback. Old filesystem mutation journals are not the v2 authority. |
| `test_batch_removal.py` / `test_mixed_batch_removal.py` | `test_storage_batch_removal.py`, `test_storage_removal_entrypoints.py` and `test_application_removal.py` verify aggregate dependency review, stale confirmations, changed worksheet sets, cancelled/partial prefixes, projection replay and retained originals/history. Mixing a hash registry with native sources is unsupported; v2 refuses that KB before mutation. |
| `test_refresh_boundaries.py` | `test_storage_freshness.py`, `test_storage_refresh.py`, `test_storage_refresh_entrypoints.py`, `test_storage_selected_refresh.py` and `test_storage_publications.py` cover excluded inputs, reviewed ancestry, conflicting heads, native source bindings and fixed readers. Directly editing old catalog/proposal JSON and deleting historical directories is retired; native reference reclamation is tested by `test_storage_maintenance.py`. |
| `test_retained_reprocessing_query.py` | `test_storage_reprocessing.py`, `test_storage_reading.py`, `test_storage_questions.py` and `test_storage_source_entrypoints.py` cover deleted external originals, failed reprocessing, exact historical inputs, managed images and refusal to substitute a different unit. No private per-publication SQLite or source-catalog JSON remains. |
| `test_knowledge_scope.py` / `test_publication_baseline_desktop.py` | These required compatibility reads or initialization of a v1 Wiki. V2 creation publishes an empty native baseline immediately and ordinary opens refuse old stores. `test_public_storage_activation.py`, `test_public_storage_failures.py`, `test_desktop_io.py` and `test_storage_repair.py` cover native creation, fixed reading, competing work and explicit navigation repair. |
| `test_source_admission.py` / `test_compile_termination.py` and file coordinator cases in `test_add_command.py` | Native catalog/import/compiler tests cover independent source identity, durable admitted originals, publication failure, terminal errors and conservative retry. `test_storage_catalog.py`, `test_storage_ingestion.py`, `test_storage_compiler.py`, `test_storage_compiler_outcomes.py` and `test_storage_native_task_budget.py` use actual SQLite/CAS and SDK traffic. The removed outer file transaction and compiler factory restart loop no longer own these operations. |
| `test_public_knowledge_protocol.py` | `test_storage_publications.py`, `test_storage_conversation_writes.py`, `test_unit_publication_process_recovery.py`, `test_publication_durability.py` and `test_publication_reads.py` retain public version closure, crash recovery, paid-result survival, fixed image/original reads and stale callbacks. Nested filesystem rollback and copied-directory identity rebinding are retired; native restores receive a new database generation. |
| `test_usage_lifecycle.py` / `test_usage_snapshot_reads.py` and JSON record-shape cases in `test_import_usage.py` | `test_storage_usage.py`, `test_storage_usage_audit.py`, `test_storage_model_execution.py`, `test_storage_watch_identity.py` and actual TCP cancellation tests check bounded receipt updates, identity validation, deletion protection, late results and phase leases. JSON ledger decorators and projection file-read counts are obsolete. |
| `test_failed_summary_retry.py`, `test_interrupted_compile_recovery.py`, `test_summary_retry_delivery.py` and `publication_recovery_inputs.py` | Their handwritten v1 task/usage/publication recovery inputs are replaced by `test_storage_compiler.py`, `test_storage_compiler_outcomes.py`, native `test_compile_checkpoints.py`, `test_summary_budget_recovery.py`, `test_index_response_reuse.py` and `test_storage_indexing.py`. These retain one repair allowance, accepted summary children, received response recovery, unknown-send refusal and index reuse independently of a publication. |
| V1 cases in `test_multi_format_integration.py`, `test_phase_budget.py`, `test_capacity_repair.py`, `test_review_regressions.py`, `test_unbounded_model_budget.py` and `test_pending_imports.py` | Native format/capability guards, persisted stop, cumulative finite/unlimited budgets and original deadlines are covered by `test_storage_database.py`, `test_public_storage_failures.py`, `test_storage_pending_stop.py`, `test_storage_native_task_budget.py`, `test_storage_model_execution.py` and `test_storage_pending_entrypoints.py`. Pure plan, capacity, tokenizer and provider error tests remain. |
| Bare-format compatibility cases in `test_source_contract_compatibility.py`, `test_reprocessing.py`, `test_text_model_boundary.py` and `test_image_delivery.py` | No v1 source inference or old image-tool-history migration is supported. Current native range, image and text-only model contracts remain covered by these modules and `test_storage_reading.py`, `test_storage_retained_inputs.py`, `test_storage_reference_images.py` and `test_storage_compile_figures.py`. |
| `test_condb_lifecycle.py` and the per-document factory fault in `test_condb_pageindex.py` | `test_storage_indexing.py`, `test_storage_trees.py`, `test_storage_reading.py` and `test_storage_publications.py` cover actual shared indexes, immutable input bindings, ready-result reuse after publication failure, independent deletion and corruption. Per-publication database copies and mutable package manifests are retired. Independent SDK package sealing, relocation and read-only round trips remain in `test_condb_pageindex.py`. |
| `test_selected_refresh_proposals.py` | `test_storage_selected_refresh.py` and `test_storage_refresh.py` cover immutable candidate bounds, manual annotations, exact reviewed inputs and stale acceptance. Hand-editing v1 candidate JSON and regenerating its hashes cannot represent an accepted native proposal. |
| V1 discovery policy/commit cases in `test_ooxml_recovery_boundaries.py`, `test_pending_imports.py` and `test_pending_recovery.py` | `test_storage_discovery.py`, `test_storage_discovery_controls.py`, `test_storage_pending.py` and `test_storage_pending_stop.py` use actual transactions, lost acknowledgements, exact claims and independent result receipts. Editing legacy intent JSON or a `MutationSnapshot` marker is retired. |
| Hand-authored task/budget continuation in `test_summary_length_recovery.py` and reprocessing request JSON in `test_reprocessing.py` | `test_summary_budget_recovery.py`, `test_storage_compiler.py`, `test_storage_compiler_outcomes.py` and `test_storage_reprocessing.py` retain accepted children, repair limits, original policies, changed-source refusal and explicit request recovery. A fabricated spent counter is not proof of a paid request. |
| Unmapped hash-registry and v1 mapping cases in `test_add_command.py` / `test_knowledge_snapshots.py` | `test_storage_catalog.py` and `test_public_storage_failures.py` check independent native source identity and refusal of mixed/old formats before writes or model requests. Native historical original and page reads remain in `test_knowledge_snapshots.py`. |
| V1 private-file corruption cases in `test_pdf_readback.py`, `test_recompile_saved_processing.py`, `test_binary_office_recovery.py`, `test_pending_recovery.py` and `test_reprocessing.py` | `test_storage_reading.py`, `test_storage_reference_images.py`, `test_storage_indexing.py`, `test_storage_discovery.py`, `test_storage_pending.py`, `test_storage_workbooks.py` and `test_storage_reprocessing.py` cover invalid native references, retained originals and images, ready-index damage, exact result ownership and frozen complete inventories. Missing old JSON/catalog/index directories and compatibility with an old discovery policy are not v2 fault seams. Independent PageIndex cache/parser tests remain. The obsolete inline-file cleanup and rollback-marker tests in `test_knowledge_refresh.py` / `test_ingest_recovery.py` map to explicit native GC/root retirement and publication/projection failure matrices; v2 never guesses ownership by scanning a v1 file tree. |

## Ported behavior tests

- `test_storage_source_files.py` opens the original and converted PDF through
  actual Qt after the external original disappears and the native source head
  changes. Both files come from the selected immutable unit; closing the reader
  cancels GC waits and cleans only its owned temporary copies.

- `test_storage_conversion_provenance.py` uses actual Word conversion and SDK
  loopback compilation to retain provenance and converted PDF bytes after the
  external original is deleted. An explicit retained input refuses receipts with
  a different original or PDF digest, while the prior publication stays readable.

- `test_knowledge_proposals.py` exercises shared, CLI, authenticated HTTP, spawned
  worker and actual Qt proposal review using real SDK loopback responses and native
  candidate storage. Review and acceptance never resend accepted model work.
- `test_pending_empty_html.py` retains an explicit zero-send `not_imported` receipt
  for empty recovered HTML; readable embedded HTML still enters the ordinary compiler.
- `test_workbook_review_regressions.py` terminates processing between actual sheet
  publications. All sheets are planned first; retry preserves the accepted sheet.
- `test_storage_generation_scope_recovery.py` checks distinct task units, repeated
  retries after ordinal changes, fixed input publications, cancellation after local
  acceptance and backup restoration with new execution identities and unchanged budgets.
- `test_storage_artifacts.py` checks that restored master bytes can recreate an
  already-applied artifact copy without a model request.

- `test_recompile_saved_processing.py` and `test_pdf_layout_versions.py` compare
  actual immutable processing metadata and CAS bytes, including old unit revisions
  after explicit reprocessing, rather than scanning a removed cache directory.
- `test_selected_refresh_sdk.py` / `test_selected_refresh_originals.py` use retained
  native document identities, fixed page input revisions and managed original reads.
- `test_compile_plan_recovery.py` uses native work and actual SDK sends, including
  the accepted-plan regressions when document index identities change.
- `test_saved_summary_replay.py` retains the captured literal/fence assertions;
  an actual local failed request exhausts the native budget before replay.
- `test_summary_budget_recovery.py` checks nested accepted batches and received
  response recovery against real native checkpoints.
- `test_summary_reference_delivery.py`, `test_reconciliation_recovery.py` and
  `test_stale_page_recovery.py` keep literal source identities, bounded coordination,
  manual-edit conflicts and repair flags in native checkpoints.
- `test_checkpoint_transactions.py` exits a real child process after a durable
  accepted computation, then verifies it survives a conflicting publication.
- `test_compile_windows.py` uses real retained inputs and native publication
  fixtures for unchanged/related provenance and sparse worksheet coordinates.
- `test_chat_slash_commands.py`, `test_skill_chat_slash.py` and
  `test_deck_chat_slash.py` use native terminal state and explicit published inputs.
- `test_application_model_errors.py` keeps actual SDK 401/503/stop coverage through
  shared use cases and JSON/SSE HTTP. Native progress and terminal totals use indexed
  counters; request IDs are queried through bounded detail pages.
- `test_markdown_readback.py` selects the exact retained unit for unpublished
  input and reads image bytes through the retained-image interface.
- `test_public_storage_activation.py` exercises the actual CLI, authenticated
  HTTP API, spawned worker, Qt main window and bounded conversation history.
- `test_storage_repair.py` checks read-only diagnosis, explicit navigation
  repair, settings recovery and paused external edits.
- `test_storage_source_entrypoints.py` checks explicit unpublished reads in the
  shared use case, CLI and authenticated HTTP, including CAS image delivery.

- `test_unit_publication_process_recovery.py` exits a real child before/after
  Blob installation, publication commit and working-copy replacement/deletion;
  replay keeps a complete published version and accepted compiler/index work.
- `test_publication_durability.py` checks failed deletion-directory sync and replay
  before projection acknowledgement.
- `test_page_transactions.py`, `test_remove.py` and `test_application_removal.py`
  use native publications for edit conflicts, symlink safety, name selection and
  original/history retention after withdrawal.
- `test_storage_watch_identity.py` and `test_cli_watch.py` reject replaced database
  identities and allow other setup while the real import waits on its model.
- `test_turn_commit_integration.py` preserves authored history and one pending
  original across real HTTP failures; explicit retries retain their original budget.


- `test_workbook_lifecycle.py` keeps sheet rename/reorder, complete-inventory
  exclusion, failed/objects-only preservation and historical reads. Its removed
  `MutationSnapshot` commit-marker fault is covered at the native commit,
  acknowledgement and projection boundaries in `test_storage_worksheet_retirement.py`.
- `test_workbook_outcomes.py` distinguishes failed current inventory from retained
  successful historical reading; the failed original remains readable from CAS.
- Native `test_recompile.py`, `test_checkpoint_import_paths.py`,
  `test_import_capacity_recovery.py` and `test_index_capacity_recovery.py` use
  retained originals, actual SDK indexes, shared budgets and real spawned workers.
- Native `test_storage_summary_retry.py` covers explicit failed-response opt-in
  through the shared use case, CLI, authenticated HTTP and spawned worker. Ordinary
  retry cannot spend a second repair; consent cannot reset the original budget.
- The removed `_snapshot_add_paths` assertion in `test_mutation.py` described v1
  hash-registry snapshots. `test_storage_compile_workspace.py` covers native
  preparation and attachment selection; no registry snapshot exists in v2.
- The removed compiler-factory restart cases in `test_unbounded_model_budget.py`
  and `test_toc_body_recovery.py` patched deleted v1 coordinators. Pure unbounded
  budget and physical no-progress algorithms remain; native cross-stage budgets,
  actual SDK request recovery and ready-index retention are covered by
  `test_storage_native_task_budget.py`, `test_storage_indexing.py` and
  `test_checkpoint_import_paths.py`.

This is a coverage migration record, not a declaration that the complete issue
has passed its final test and release gates.

## Native acceptance commands

The v1 `scripts/verify_retained_summary.py` and `scripts/verify_create_capacity.py`
replayed file manifests and fabricated model-task state. Their native replacements
are `test_retained_summary_reuse.py`, `test_storage_summary_retry.py`,
`test_create_capacity.py`, `test_create_assembly.py` and the native capacity recovery
tests listed above.

The v1 `scripts/verify_compile_acceptance.py` response-directory relocation and
`scripts/verify_usage_projection.py` JSON projection benchmark are retired.
Compiler acceptance now uses `test_storage_compiler.py`,
`test_storage_compiler_outcomes.py`, `test_compile_acceptance.py` and
`test_checkpoint_transactions.py`. Native callback scaling/audit uses
`scripts/verify_storage_usage.py`; revision/CAS growth uses
`scripts/verify_storage_revisions.py`. These commands operate on explicit native
fixtures and never reinterpret a copied v1 directory as a supported knowledge base.
