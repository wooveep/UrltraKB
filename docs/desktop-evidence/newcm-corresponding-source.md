# Bundled font source and licence evidence

Checked 2026-09-08 against both frozen `87bdcc7` inventories. This note
distinguishes the font resources actually redistributed from the upstream
process that generated them. It does not claim that a generic font-generation
tool must become part of OpenKB's Corresponding Source merely because OpenKB
ships its unmodified output.

## Disposition

- **NewCM licence identification is resolved:** retain the npm package's
  Apache-2.0 declaration and separately include the GUST Font License, its
  referenced LPPL 1.3c, and the font copyright statements preserved inside the
  actual WOFF2 files. A single Apache-2.0 label omits this font-resource layer.
- **The complete, unmodified NewCM npm package is available and verified.**
  Both programs contain all 1,124 files from version 4.1.3 with identical
  content. Preserve that complete upstream package in the redistribution
  materials, including its definition file and readable JavaScript tables.
- **Exact upstream regeneration / preferred editable font source remains
  unestablished.** Do not label the candidate NewCM archives below as exact
  corresponding source, or claim that `def/mathjax-newcm.ts` alone can recreate
  the released resources. This evidence limitation is distinct from the
  permission to redistribute an unchanged font package under its own terms.
- **Noto Sans CJK is an unchanged, independently licensed font resource.** Its
  original OTF files, copyright statements and full OFL are the relevant
  redistribution materials; no font-source or font-generator obligation
  follows merely from bundling those files with the application.

## Exact NewCM files and terms

The npm registry identifies `@mathjax/mathjax-newcm-font@4.1.3`, git head
`b3b77f655ed2d9009078301626e27b56dcfa90d9`, package licence `Apache-2.0`, and
1,124 files. The cached tarball passes both published SHA-1 and SHA-512
integrity values. Every member matches the Debian and Windows frozen
inventories, with no missing, additional or changed files. The package has
392 JavaScript files, 308 declaration files, one implementation definition
file, 308 maps, 105 WOFF2 fonts, eight JSON files and two examples.
[Exact publisher metadata](https://registry.npmjs.org/@mathjax/mathjax-newcm-font/4.1.3),
[exact upstream tarball](https://registry.npmjs.org/@mathjax/mathjax-newcm-font/-/mathjax-newcm-font-4.1.3.tgz).

FontTools inspection of all 105 WOFF2 `name` tables finds the original
2019–2021 Antonis Tsolomitis copyright and an explicit GUST Font License
reference in every file. Twenty-one additionally carry MathJax, Inc.'s 2024
modification copyright. The per-file, verbatim metadata is preserved in
`font-resolution/bundled-woff2-metadata.json`; it comes from the exact npm
tarball above, rather than a current CTAN package's unrelated version label.

The referenced GUST text incorporates LPPL 1.3c or later. LPPL distinguishes
complete unmodified redistribution (clause 2), compiled works (3 and 7),
derived works (6), and unrelated aggregation (11). Therefore the materials
should preserve the complete upstream font package and its own terms. A
claim about a rebuild from earlier font inputs needs separate evidence;
neither npm's package-level licence string nor a missing build tool settles
that question.
[GUST licence](https://tug.org/fonts/licenses/GUST-FONT-LICENSE.txt),
[LPPL 1.3c](https://www.latex-project.org/lppl/lppl-1-3c/).

## Editable data and the remaining provenance boundary

The exact package supplies `def/mathjax-newcm.ts` and readable `mjs`/`cjs`
glyph metrics and SVG path tables. Its `.d.ts` files are declarations, and
its source maps do not include `sourcesContent`. The definition names ten
NewCM OTF inputs plus `MJX-Extra-Regular.otf`; those original inputs are not
members of this npm package. There is no independent LICENSE/NOTICE member
in the font npm tarball. The separately cached Apache text is taken from the
same publisher's exact `@mathjax/src@4.1.3` LICENSE; the GUST and LPPL texts
come from the URLs named by the fonts.
[Exact font package](https://registry.npmjs.org/@mathjax/mathjax-newcm-font/-/mathjax-newcm-font-4.1.3.tgz),
[MathJax 4.1.3 source package](https://registry.npmjs.org/@mathjax/src/-/src-4.1.3.tgz).

GitHub's API returned 404 for the declared MathJax-fonts repository and the
registry's exact git head. This establishes that they were unavailable
through the checked public route, not that no source exists. A MathJax
maintainer also described the font tools as not yet public in August 2025;
that historical statement is not a claim about their status today and is
not by itself a redistribution blocker.
[Declared repository](https://github.com/mathjax/MathJax-fonts),
[maintainer statement](https://github.com/mathjax/MathJax/issues/3075#issuecomment-3191273608).

Two author-hosted historical archives were retained as candidates only.
Across 23,454 glyph records in the bundled WOFF2 files, exact decomposed
drawing-operation comparison found 21,999 matches against the ten relevant
NewCM 5.02 OTFs and 22,245 against 7.0.4. Neither comparison verifies a
complete match, font version, metrics, layout tables, or preferred source
form. Differences can reflect MathJax edits, a different upstream version,
or operation normalization. These archives contain OTFs and documentation,
without SFD members. No candidate was substituted into the product.
[Author's release directory](https://download.gnu.org.ua/release/newcm/),
[5.02](https://download.gnu.org.ua/release/newcm/newcm-5.02.txz),
[7.0.4](https://download.gnu.org.ua/release/newcm/newcm-7.0.4.txz).

If the final release promises regeneration of these third-party font
resources, the remaining concrete item is a verified mapping to the actual
font inputs/modifications, or an established preferred editable source
form for the shipped resource. Keep this separate from the already
available program source and the unchanged-font redistribution inventory.
Do not silently upgrade NewCM, change the pinned MathJax packages, or
relabel reconstructed outlines as upstream source to close the item.

## Noto Sans CJK Sans2.004

The installed Regular and Bold OTFs exactly match Git blobs from the
`Sans2.004` tag, commit `523d033d6cb47f4a80c58a35753646f5c3608a78`:

| File | Installed SHA-256 |
| --- | --- |
| NotoSansCJKsc-Regular.otf | `2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b` |
| NotoSansCJKsc-Bold.otf | `b5f0d1a190a7f9b43c310a8850630af12553df32c4c050543f9059732d9b4c0a` |

The exact tag's root LICENSE and Sans release notes establish OFL 1.1.
Keep the OTFs and copyright/OFL materials in the independent-font section,
without relabelling the fonts AGPL.
[Fixed release tree](https://github.com/notofonts/noto-cjk/tree/523d033d6cb47f4a80c58a35753646f5c3608a78/Sans/OTF/SimplifiedChinese),
[fixed licence](https://github.com/notofonts/noto-cjk/blob/523d033d6cb47f4a80c58a35753646f5c3608a78/LICENSE),
[fixed release notes](https://github.com/notofonts/noto-cjk/blob/523d033d6cb47f4a80c58a35753646f5c3608a78/Sans/NEWS.md).

OFL clause 2 permits bundling with software when the copyright and licence
are preserved. Its official FAQ 1.2 describes fonts as typically aggregated
with software; FAQ 3.6 does not impose public availability of derivative
source/build files, and FAQ 5.9 distinguishes intact-font packaging from
a modified rebuild. Thus lack of a Noto font-generator source archive is
not a missing corresponding-source item for this unchanged resource.
[OFL official text](https://openfontlicense.org/open-font-license-official-text/),
[OFL official FAQ](https://openfontlicense.org/ofl-faq/).

## Saved evidence

All supplementary files are under
`packaging/desktop/build/source-cache/font-resolution/` with sizes, SHA-256
and source URLs in `index.json`. The index explicitly marks historical
NewCM archives and current CTAN documents as research candidates, not
verified corresponding source. Suitable licence supplements are
`GUST-FONT-LICENSE.txt`, `LPPL-1.3c.txt`, `Apache-2.0-MathJax.txt`, and
`NotoSansCJK-Sans2.004-OFL.txt`. `newcm-package-verification.json` and
`noto-original-verification.json` bind the findings to the shipped files.
