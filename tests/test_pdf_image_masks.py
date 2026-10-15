"""PDF image assets preserve transparency through each public conversion path."""

import io

import pymupdf
import pytest
from PIL import Image

from ultrakb.images import (
    convert_pdf_to_pages,
    convert_pdf_with_images,
    extract_pdf_images,
)


@pytest.mark.parametrize("mode", ["retained", "pages", "markdown"])
def test_pdf_soft_mask_preserves_transparent_and_translucent_pixels(tmp_path, mode):
    image = Image.new("RGBA", (60, 40))
    for x in range(60):
        for y in range(40):
            image.putpixel((x, y), (255, 0, 0, (0, 128, 255)[x // 20]))
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    pdf = tmp_path / "masked.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((60, 60), "The figure below retains its original transparency.")
        page.insert_image(pymupdf.Rect(60, 90, 120, 130), stream=encoded.getvalue())
        doc.save(pdf)
    original = pdf.read_bytes()
    assets = tmp_path / "assets"

    if mode == "retained":
        references = extract_pdf_images(pdf, "masked", assets)[1]
    elif mode == "pages":
        pages = convert_pdf_to_pages(pdf, "masked", assets)
        references = [figure["path"] for figure in pages[0]["images"]]
        assert "original transparency" in pages[0]["content"]
    else:
        records = []
        body = convert_pdf_with_images(pdf, "masked", assets, page_records=records)
        references = [figure["path"] for figure in records[0]["images"]]
        assert "original transparency" in body

    assert references == ["sources/images/masked/p1_img1.png"]
    with Image.open(assets / "p1_img1.png") as saved:
        assert saved.mode == "RGBA"
        assert saved.getchannel("A").tobytes() == image.getchannel("A").tobytes()
        white = Image.new("RGBA", image.size, "white")
        assert (
            Image.alpha_composite(white, saved).tobytes()
            == Image.alpha_composite(white, image).tobytes()
        )
    assert pdf.read_bytes() == original


@pytest.mark.parametrize(
    "mode, color", [("L", 128), ("RGB", (255, 0, 0)), ("CMYK", (0, 255, 255, 0))]
)
def test_opaque_pdf_images_remain_visible_in_png_assets(tmp_path, mode, color):
    image = Image.new(mode, (60, 40), color)
    encoded = io.BytesIO()
    image.save(encoded, format="JPEG" if mode == "CMYK" else "PNG")
    pdf = tmp_path / "opaque.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_image(pymupdf.Rect(60, 90, 120, 130), stream=encoded.getvalue())
        doc.save(pdf)

    assets = tmp_path / "assets"
    references = extract_pdf_images(pdf, "opaque", assets)

    assert references == {1: ["sources/images/opaque/p1_img1.png"]}
    with Image.open(assets / "p1_img1.png") as saved:
        assert saved.size == image.size
        if mode == "CMYK":
            assert saved.mode == "RGB"
            red, green, blue = saved.getpixel((30, 20))
            assert red > 200 and green < 60 and blue < 60
        else:
            assert saved.convert("RGB").tobytes() == image.convert("RGB").tobytes()
