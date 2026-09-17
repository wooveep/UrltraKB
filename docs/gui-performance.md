# GUI and compilation memory performance

The September 17 performance follow-up addresses slow first access to the source
inventory and retained objects in long desktop sessions and compilations.

## Inventory reads

`get_kb_list()` uses the summary view of `source_status()`. It still validates
saved outcomes and source identity and reports accumulated usage, but does not
load navigation trees, individual cloud jobs or local OCR detail records. Full
source details remain the default for other callers and are loaded when opened.
The already validated latest run is reused in the history sum. The summary view
projects only the result fields used by inventory instead of recursively copying
the complete request accounting payload.

A read-only, fresh-process `cProfile` measurement on the Windows test machine's
15-document `cloudyiKB` gave:

| Measurement | Before | After |
| --- | ---: | ---: |
| Inventory read | 12.252 s | 1.892 s |
| Process peak working set | 319.75 MiB | 97.59 MiB |
| Working set after read | 283.79 MiB | 53.71 MiB |

These are single observations of the inventory service, not a GUI frame-latency
guarantee or a bound for arbitrary knowledge-base sizes. The baseline included
cold imports needed only by navigation. Empty-KB first navigation took 41 ms in
the separate offscreen Workbench probe.

## Object and checkpoint lifetime

- Switching knowledge bases closes and deletes retired workspace panels, including
  their Qt tables and documents, and stops pending Markdown rendering.
- Closing a source-review dialog deletes its Qt storage and original-page image.
  Closed views reject late background-read callbacks.
- Completed checkpoint records are decoded on demand, not retained for the entire
  compilation. Returned values still belong to their caller.
- Pending request contracts use private process-local temporary files. Saving an
  immutable checkpoint transfers the complete contract to the existing durable
  format and discards its temporary copy. Normal owner cleanup removes remaining
  scratch files; scratch files are not a substitute for durable recovery records.
- Appending a checkpoint updates the compact stage index without decoding all
  previous prompts. Missing or invalid indexes are rebuilt from validated records;
  legacy indexes without stage classification remain eligible for recovery.

Sixteen 1 MiB pending contracts retained 16,802,374 traced bytes before the change
and 38,121 afterward. Reading sixteen saved contracts retained 16,909,947 bytes
before and 944 afterward. The tests allow up to 4 MiB to avoid allocator-specific
expectations and verify that old requests can still be published and restored.
These figures concern retained Python allocations, not total worker RSS.

A deterministic 60-topic compilation completed with 123 offline model responses
(2 facts, 1 planning, 60 generation, 60 verification). Retained allocations grew
about 0.40 MiB between topics 10 and 60 after the change, compared with 1.78 MiB
before. A separate 65-request local streaming control retained about 1.52 MiB;
it did not reproduce an SDK response leak. No paid models or OCR were used.

The earlier 508-page task's saved result reports `MemoryError`, but its available
log does not identify the allocating stack. The fixes address reproduced growth
points; they do not establish the sole cause of that historical failure or prove
every possible import has a fixed memory ceiling.

## Review reasoning

Previously saved four-case trials found the concise disabled-reasoning reviewer
took 5.8 s total and classified 2/4 cases correctly. The revised concise prompt
with low reasoning classified 4/4 in 155.7 s; high reasoning classified 4/4 in
193.1 s. These were single runs on prompt-tuning cases, not independent validation.
Disabling reasoning is technically possible and faster in those samples, but
has insufficient correctness evidence for a global review/correction default.
This performance change leaves the configured model reasoning controls intact.

## Regression seams

`tests/test_inventory_performance.py` exercises the saved-source inventory and
checks that optional detail reads remain available only when requested.
`tests/test_desktop_panel_lifetime.py` checks retired Qt objects and late callbacks.
`tests/test_checkpoint_memory.py` checks large pending/resumed inputs, append-only
index work, legacy/corrupt/missing index restoration and returned-value isolation.
Source-flow, source-review, document-resume and shared-analysis tests cover their
surrounding workflows.

Local measurement receipts are kept outside version control in
`packaging/desktop/build/gui-performance-20260917/`. Windows build, installation
backup, configuration-preservation and launch receipts are stored separately in
`packaging/desktop/build/windows-gui-performance-20260917/`.
