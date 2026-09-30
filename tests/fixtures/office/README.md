# Synthetic Office fixtures

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
