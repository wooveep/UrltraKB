"""Retain native page evidence before optional model navigation is attempted."""

from pathlib import Path

from ultrakb.state import HashRegistry
from ultrakb.unit_publication import copy_tree


def retained_pdf_pages(source, doc_name, stages, images_dir, *, extract, normalize):
    if source.suffix.lower() not in {".pdf", ".okpi"}:
        return None
    saved_images = stages.directory / "original-images"
    cached = stages.load("stage:original-pages")
    if cached is None:
        if source.suffix == ".okpi":
            from ultrakb.office.slide_content import attach_slides
            from ultrakb.office.slide_package import materialize_slide_package

            with materialize_slide_package(source) as (snapshot, slides):
                pages = attach_slides(
                    normalize(extract(snapshot, doc_name, saved_images, allow_empty_text=True)),
                    slides,
                )
        else:
            pages = normalize(extract(source, doc_name, saved_images))
        cached = {
            "pages": pages,
            "images": {
                file.name: HashRegistry.hash_file(file)
                for file in saved_images.iterdir()
                if file.is_file()
            }
            if saved_images.exists()
            else {},
        }
        stages.save("stage:original-pages", cached)
    for name, expected in cached["images"].items():
        path = saved_images / name
        if (
            Path(name).name != name
            or not path.is_file()
            or HashRegistry.hash_file(path) != expected
        ):
            raise ValueError("Retained original page image is missing or changed")
    if saved_images.is_dir():
        copy_tree(saved_images, images_dir)
    return cached["pages"]
