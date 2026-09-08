# UrltraKB application fonts

The original, unmodified Adobe fonts in this directory are supplied by the maintainer.
`manifest.json` is the runtime whitelist: it pins each selected file's SHA256,
family, real style, weight, version and complete OFL copyright/license notice.
Other fonts in the source workspace are reference assets, not runtime inputs.

- Source Han Sans CN 2.005: static Regular (400), Medium (500), Bold (700)
  for prose, navigation/section titles and Markdown headings/emphasis.
- Source Code Pro 2.042: Regular (400), Medium (500), Bold (700) for code.
- Source Code Pro 1.062: Italic and Bold Italic for emphasized inline code.

Fonts are loaded with Qt's application font database; no system installation is
required. Do not pin `styleName` or a variable `wght` axis on the base QFont:
those properties prevent inherited QSS/Markdown weights from selecting real faces.
Code uses an explicit Source Han Sans fallback for Chinese at every weight.

The source tree loads this directory. Wheel and frozen builds include only the
manifest's fonts, their notices and this manifest/README, under
`openkb/desktop/assets/fonts/`. Keep the wheel's explicit force-include list in
sync with the manifest; the desktop spec derives its whitelist from it.
Native diagram rendering retains its separately pinned Noto resources/notices.
