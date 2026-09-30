# Synthetic Office fixtures

The `package-text.docx`, `.pptx`, and `.xlsx` files are the small external
`embedded-simple-2007` fixtures recorded in `package-fixtures.json`, under the
adjacent `oletools-fixture-LICENSE.md`. Each holds one standard Ole10Native
Package containing a short benign ASCII sentence. All other fixtures described
below are authored test data.

`writer.doc` is a real Word 97 binary file, generated with the pinned private
LibreOffice 26.2.6.3 build from the small `writer_document` DOCX fixture in
`tests/test_office_import.py`. It contains three physical pages (page 2 blank),
tracked insertion/deletion, hidden text, Chinese text and a 160 × 80 white image
with a red rectangle. The image is anchored as a character at the document end,
with a 30 × 15 mm display size. The generator saved using the `MS Word 97` filter.

`encrypted.doc` is the same document saved with that filter and the UNO `Password`
property set to `fixture-only`. It verifies explicit password rejection. These
files are authored test data, with no external or user content. Their embedded
objects are outside the scope of the body-conversion tests.

`slides.pptx` was authored using the same private Office's Impress UNO API, then
saved with `Impress MS PowerPoint 2007 XML`. It contains three slides: a visible
text page with key-rotation notes, a hidden text page with notes and the same
small rectangle image, and a blank slide containing only speaker notes. No
fixture relies on an installed desktop Office or network resources.

`slides.pptx` 的第 2 页图片下还有 `AFTER_IMAGE_MARKER`，用于核验索引与来源正文的字符定位一致。

`slides.ppt` 是相同三页合成内容通过固定 LibreOffice 26.2.6.3 的 `MS PowerPoint 97`
过滤器独立保存的二进制文件；第 2 页隐藏且含图片，第 3 页只有演讲备注。

`typed-sheets.xls` is independently authored BIFF8/CFB test data saved with the
private LibreOffice 26.2.6.3 `MS Excel 97` filter. Alpha contains A1 text `0012`,
B2 numeric 42 with zero-padding format, D2 date 2025-01-02, E2 formula `=B2+1`
(cached 43), F2 `="cached text"`, G2 `=TRUE()` and IV65530 text `XLS_TAIL_VALUE`.
Row 2 and column B are hidden. Beta contains `XLS_BETA`; hidden Gamma has C5
`XLS_GAMMA`. Calculation happened only while authoring this fixed fixture; the
importer never invokes Calc or calculates formulas. No real user content.

`negative-sheets.xls` is the same authored BIFF8 fixture with B2's RK numeric
record changed to -42, preserving the `00000` mask. Its cached E2 result remains
43 deliberately: stored caches are evidence, not a promise of recalculation.
It verifies that signs do not consume a zero-padding digit.

`embedded-calc.doc` and `embedded-writer.doc` were authored with the pinned Office
UNO API: a Writer `TextEmbeddedObject` uses Calc CLSID
`47BBB4CB-CE4C-4E80-A591-42D9AE74950F` or Writer CLSID
`8BC6B165-B1B2-4EDD-AA47-DAE2EE689DD6`. The contained model has the literal
`EMBEDDED_CALC_STANDARD_MARKER` in A1 or `EMBEDDED_WRITER_STANDARD_MARKER` in its
text. Saving the outer Writer with `MS Word 97` creates actual ObjectPool CFB
substorages with standard Workbook or WordDocument streams.

`embedded-metadata.doc` extends the Calc fixture using cfb 0.15.0 as an independent
fixture writer. Its selected storage has state `0x12345678` and creation timestamp
1700000000 Unix seconds, modified one second later. Nested has the same CLSID,
creation time, modification two seconds later, an Empty storage, Small stream
`nested fixture bytes` (state `0x1357`), and Large stream of 9000 bytes `0xA5`.
The tests check these literal values through olefile and require the rebuilt root
creation time to be zero. The underlying workbook and its marker stay unchanged.

`embedded-nested.doc` places the complete Calc fixture's standard storage under
the embedded Writer's own `ObjectPool/_nested`, using pinned cfb 0.15.0 to copy
all streams and metadata. The deliberately orphaned nested Calc must be imported
once, by the recovered Writer's discovery, rather than also by the outer host.
