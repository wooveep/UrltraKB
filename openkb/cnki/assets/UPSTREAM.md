# CNKI conversion provenance

The CAJ header layout, embedded PDF object discovery, missing `/Pages`
containers and KDH `FZHMEI` XOR decoding derive from
[caj2pdf commit 6c4bc32b15ce748d211f45d536f5d5511ef9f368](https://github.com/caj2pdf/caj2pdf/tree/6c4bc32b15ce748d211f45d536f5d5511ef9f368).
Its GLWT license is retained in `GLWT-LICENSE.txt`.

OpenKB's compatibility patch reconstructs a PDF from complete, consistent
objects. A truncated duplicate is discarded only when a complete copy of that
same object exists. Missing page containers are reconstructed from original
parent references, retaining physical page order. The final PDF has an explicit
xref and catalog; incomplete unique objects, conflicting complete copies,
unsupported structures and page-count mismatches fail. This fixes the local
58-page CAJ regression without publishing the baseline's damaged `pdf.tmp` or
swallowing a failed native cleaner.

KDH decodes its embedded PDF. A missing LF after a CR stream marker is rewritten
by the pinned native PDF serializer, then checked again; other structural
warnings fail. Font ascent/descent advisories are recorded for visual review.
PDF-signature inputs are validated directly.
HN, C8, TEB and unknown signatures are unsupported. There is no OCR, network
converter, system `mutool`, or external reader dependency.

`runtime-lock.json` records the exact PyMuPDF version, MuPDF version, source
archive and Windows/Linux x64 wheel hashes from `uv.lock`. PyMuPDF already
supplies the application's PDF parser; its native runtime and license materials
ship in the frozen package. Each conversion receipt additionally records hashes
of the executing helpers and native libraries. Conversion runs in one disposable
process, with private temporary files, bounded execution and strict final PDF
validation. The PDF and receipt enter the existing normalization transaction.

Automated tests use small generated fixtures. Real papers remain local acceptance
inputs and are never distributed with OpenKB. Package execution and visual
acceptance results belong in `docs/internal/validation/`; parsing success does
not constitute a claim of complete visual fidelity.
