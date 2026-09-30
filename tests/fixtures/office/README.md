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
