"""The pinned Office fallback renders a cross rather than the old star glyph."""

from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from openkb.office.fontconfig import FONT_FILES, require_font_supply


def test_plus_outline_has_two_orthogonal_bars():
    asset = Path(__file__).parents[1] / "assets/fonts/FrankRuhlHofshi-Bold.otf"
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=140, height=140)
        page.insert_font(fontname="office", fontfile=str(asset))
        page.insert_text((25, 105), "+", fontsize=90, fontname="office")
        pixels = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), colorspace=pymupdf.csRGB)
    dark = [
        (index % pixels.width, index // pixels.width)
        for index in range(pixels.width * pixels.height)
        if max(pixels.samples[3 * index : 3 * index + 3]) < 100
    ]
    rows = {y: sum(other_y == y for _, other_y in dark) for _, y in dark}
    columns = {x: sum(other_x == x for other_x, _ in dark) for x, _ in dark}
    row, column = max(rows, key=rows.get), max(columns, key=columns.get)
    width = max(x for x, _ in dark) - min(x for x, _ in dark)
    height = max(y for _, y in dark) - min(y for _, y in dark)
    axes = sum(abs(x - column) <= width * 0.16 or abs(y - row) <= height * 0.16 for x, y in dark)
    assert axes / len(dark) > 0.9


def test_old_office_font_supply_requires_an_explicit_rebuild():
    with pytest.raises(ValueError, match="rebuild"):
        require_font_supply(SimpleNamespace(files={}))
    require_font_supply(SimpleNamespace(files=FONT_FILES))
