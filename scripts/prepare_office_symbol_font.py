"""Build the finite Office plus-glyph correction from pinned runtime fonts.

Use `uv run --with fonttools==4.59.2 python scripts/prepare_office_symbol_font.py
--fonts <original-private-runtime>/share/fonts/truetype --output assets/fonts`.
The original font has no Reserved Font Name in its OFL notice. Keeping its
family and metrics preserves Office's existing Latin/CJK fallback and layout.
"""

import argparse
import hashlib
from pathlib import Path

INPUTS = {
    "FrankRuhlHofshi-Bold.otf": "738df99ce285d69e83e2cd2809c340591d805f4af9b2609ca9ec50c14c3f9691",
    "LiberationSerif-Bold.ttf": "d754ba427cfe0bca54ae052384baa8f842da5bd6550ad4da024ac441e7a7d5ce",
}


def prepare(fonts: Path, output: Path) -> None:
    import fontTools
    from fontTools.misc.transform import Transform
    from fontTools.pens.t2CharStringPen import T2CharStringPen
    from fontTools.pens.transformPen import TransformPen
    from fontTools.ttLib import TTFont

    if fontTools.__version__ != "4.59.2":
        raise ValueError("Office symbol asset generation requires fonttools==4.59.2")
    for name, checksum in INPUTS.items():
        if hashlib.sha256((fonts / name).read_bytes()).hexdigest() != checksum:
            raise ValueError(f"Office symbol font input changed: {name}")
    with (
        TTFont(fonts / "FrankRuhlHofshi-Bold.otf", recalcTimestamp=False) as target,
        TTFont(fonts / "LiberationSerif-Bold.ttf", recalcTimestamp=False) as source,
    ):
        glyph = target.getBestCmap()[0x2B]
        top = target["CFF "].cff.topDictIndex[0]
        pen = T2CharStringPen(width=target["hmtx"][glyph][0], glyphSet=source.getGlyphSet())
        scale = target["head"].unitsPerEm / source["head"].unitsPerEm
        source.getGlyphSet()[source.getBestCmap()[0x2B]].draw(
            TransformPen(pen, Transform(scale, 0, 0, scale, 0, 0))
        )
        top.CharStrings[glyph] = pen.getCharString(
            private=top.Private, globalSubrs=target["CFF "].cff.GlobalSubrs
        )
        for record in target["name"].names:
            if record.nameID == 0:
                record.string = (
                    record.toUnicode()
                    + " U+002B outline: "
                    + source["name"].getDebugName(0)
                    + " Modified by OpenKB: plus outline only; original advance and metrics."
                ).encode(record.getEncoding())
        output.mkdir(parents=True, exist_ok=True)
        target.save(output / "FrankRuhlHofshi-Bold.otf")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fonts", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    prepare(args.fonts, args.output)
