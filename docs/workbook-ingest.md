# Workbook sources and worksheet units

An XLSX is one retained Source. Each worksheet has its own processing target,
frozen Markdown, token classification, optional content-block index, publication
and actual successful revision. One failed worksheet leaves the others published.
Importing the same revision retries unfinished units and skips committed ones.

`openkb list` shows worksheet identities beneath the source. Read a sheet with
`openkb source SOURCE_ID --unit UNIT_ID --cells 'A1:B3,D7'`; omit `--cells` for its
full frozen body. `openkb retry-worksheet SOURCE_ID --unit UNIT_ID` retries only
that retained target. `openkb recompile SOURCE_ID --unit UNIT_ID` uses the saved
normalization and index. The API source/recompile requests accept `unit_id`; the
source request accepts `cells`. `/api/v1/document/retry-worksheet` accepts
`source_id` and `unit_id`. Desktop reading opens the worksheet table, with read,
retry and recompile actions. Task status can be `partial`.

The direct dependency is openpyxl 3.1.5. Its versioned `ExcelReader`, workbook
inventory and `WorkSheetParser.parse` read stored cells without the padded
rectangles of `iter_rows` or expanded merged-cell objects. Formula and cached-value
passes are separate. No formula evaluation, macro execution or external-link
update occurs. Type, number format, ISO date, hidden row/column, merged ranges,
formula and cache presence are retained with physical cell coordinates. Simple
zero-padding formats preserve leading zeros; number-format metadata is retained
without claiming a complete Excel display renderer. Distant formatting does not
invent data; distant actual values remain included. A partial character/block
read removes full cell values from clipped metadata.

Cell ranges are finite, inclusive A1 ranges, may be disjoint, and select only
stored content. Frozen normalized character spans remain the common indexing
coordinate; worksheet/cell locators map those spans to original evidence.
Historical views list the names and units of their own publication snapshot.

Verification: real three-sheet XLSX imports, per-sheet model failure and retry,
sparse typed/formula fixtures, API/CLI reads, retained-input single-sheet retry,
segmented indexing/offline recompile, pinned-history reads and version-wait resume.
A spawned runtime worker uses a local model-boundary HTTP fixture to verify partial
task status and retry. Qt offscreen checks cover table rendering and cell selection.

Rename/reorder reconciliation, confirmed-empty and deleted-sheet retirement are
covered by the subsequent worksheet lifecycle implementation (#93).

## Binary XLS

XLS uses the same worksheet publication/read/retry paths. xlrd is a direct,
exactly pinned dependency at 2.0.2 (already present in the conversion dependency
closure). Its [versioned upstream documentation](https://github.com/python-excel/xlrd/blob/2.0.2/README.rst)
explicitly distinguishes cached formula results from expressions. We retain
BIFF formula-record presence through an isolated `Book` subclass, mark those
expressions `unavailable`, and expose numeric/string/boolean/error/date caches
with their actual types. No formula decompiler, macro engine, global monkeypatch
or Office process runs during XLS ingestion. The pinned loader follows xlrd's
BIFF2–8 branches; a real BIFF8 file is the independently authored acceptance
fixture. Non-cell sheet kinds are explicit failed units, never a valid empty table.

`ragged_rows=True` avoids extending every row to the furthest column; stored
format-only cells are excluded from body content. Physical distant values remain.
XLS lacks an OOXML sheet ID: initial name-derived keys are reconciled against
retained identities by the common lifecycle in #93. They are not content hashes
or ordinal-only identities.

The pinned [upstream license](https://github.com/python-excel/xlrd/blob/2.0.2/LICENSE)
is retained as `openkb/workbooks/xlrd-LICENSE.txt`. This product includes software
developed by David Giffin <david@giffin.org>. The subclass/loader adapter is based
on Stephen John Machin's BSD-licensed xlrd loader; xlrd owns CFB and BIFF parsing.
