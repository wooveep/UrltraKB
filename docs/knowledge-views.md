# General knowledge base

Documents compile into the shared `wiki/` in their knowledge base. They can
cover unrelated topics, have arbitrary titles, and omit version labels. Ordinary
import does not classify them as product manuals or require product aliases,
applicable versions, document families, or metadata confirmation.

```bash
openkb add notes.pdf
openkb add budget.xlsx
openkb query "What do these documents say about the project?"
```

Query, chat, CLI, HTTP and desktop use the same source readers. Product names and
version strings in a question are search terms. Source content determines their
meaning; a separate catalog does not decide whether the question may proceed.
Generated answers retain their citations and code without a required
fact-registration protocol or another model's final review. Factual quality must
still be evaluated against the original documents.

The project is in development. Rebuild knowledge bases with this import flow;
this change supplies no migration or compatibility workflow for libraries
previously split into product/version partitions. The desktop has no version
selector, metadata correction form, family-default dialog or old-library mapping.
The internal shared-wiki identifier remains `legacy`; it describes a storage path,
not an age or product version of the imported material.

Source revisions, frozen original coordinates and read-path checks remain.
Knowledge writes use the lock, mutation and immutable-publication mechanisms.
Manual edits produce reviewable proposals instead of being overwritten.
Malformed or incomplete model output is reported as failed compilation: removing
mandatory semantic-evidence fields does not remove structural JSON validation.
