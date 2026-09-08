# UrltraKB application fonts

These are the unmodified font files supplied for the desktop redesign.
The family, version and copyright are read from their embedded OpenType name tables.
`manifest.json` pins the original file hashes and associates each with its OFL notice.

- Source Han Sans CN VF 2.005: interface and prose. Explicitly select Regular
  (400) and set the `wght` axis to 400; selecting the style name alone can
  retain the ExtraLight instance on some Qt font backends.
- Source Code Pro 2.042 Regular: code blocks and the Markdown source editor.

Load fonts with Qt's application font database, never by installing system fonts.
The source tree uses this directory; wheel and desktop builds copy the same bytes
into `openkb/desktop/assets/fonts/`. Native diagram rendering retains its separately
pinned Noto resources and notices.
