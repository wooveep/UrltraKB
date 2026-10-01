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

## Private Office symbol fallback

`FrankRuhlHofshi-Bold.otf` is a modified OFL 1.1 font for the private Office
runtime. Only the U+002B outline is replaced by the conventional plus outline
from its bundled Liberation Serif Bold font. Original advances, metrics and
other glyphs stay intact: wholesale family aliases caused reflow or punctuation
reordering in the 103-slide regression. The complete source copyright notices
and OFL are in `OfficeSymbol-OFL.txt`. Neither source font software nor user
document text is changed in place. This derivative keeps the original family;
its Frank Ruhl Hofshi notice declares no Reserved Font Name.

Regenerate with `uv run --with fonttools==4.59.2 python
scripts/prepare_office_symbol_font.py --fonts <original-private-runtime>/share/fonts/truetype
--output assets/fonts`. The script validates both original font SHA256 values
from the pinned LibreOffice 26.2.6.3 runtime and preserves timestamps. Rebuild
the Office runtime using `scripts/prepare_office_runtime.py`; its existing
manifest copy installs the corrected font and notice. The runtime validator
requires these exact artifacts on Linux. LibreOffice itself is unchanged.

The first eight fonts above remain the application UI faces. The Office
fallback is bundled under the same manifest for reproducible packaging.
