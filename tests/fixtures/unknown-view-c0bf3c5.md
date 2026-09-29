# Previously published unknown view

`unknown-view-c0bf3c5.zip` contains the catalog, frozen original and knowledge
snapshot emitted by `import_document` at commit `c0bf3c54175c954e15ff82c3c89cfa3e2cd166b7`.
The input is a tiny PyMuPDF PDF. Only `litellm.completion` was replaced with fixed
summary/concept replies. No application or converter functions were mocked.
Configuration, locks and the caller's copy of the input are excluded.

This fixture verifies that clarification can upgrade a previously published
unknown view using its retained normalization without rerunning PDF parsing.
