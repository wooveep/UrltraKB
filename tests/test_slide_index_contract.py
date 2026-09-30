"""Frozen slide inputs keep valid original anchors throughout shared tree recursion."""

import base64
import hashlib
import json
from types import SimpleNamespace

pytest_plugins = ("test_pdf_readback",)


def slide_package(tmp_path, bodies, notes, *, cover_image=False):
    import pymupdf

    slides = []
    with pymupdf.open() as pdf:
        for ordinal, (body, note) in enumerate(zip(bodies, notes), 1):
            page = pdf.new_page()
            if body:
                page.insert_text((72, 72), body)
            if cover_image and ordinal == 1:
                pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 64, 64), False)
                pixmap.clear_with(128)
                page.insert_image(pymupdf.Rect(72, 100, 136, 164), stream=pixmap.tobytes("png"))
            slides.append(
                {
                    "ordinal": ordinal,
                    "page": ordinal,
                    "name": str(ordinal),
                    "hidden": False,
                    "notes": note,
                    "body": body + ("\n" if body else ""),
                }
            )
        data = pdf.tobytes()
    path = tmp_path / "slides.okpi"
    path.write_text(
        json.dumps(
            {
                "format": "openkb-slide-pdf-v1",
                "pdf": base64.b64encode(data).decode(),
                "pdf_digest": hashlib.sha256(data).hexdigest(),
                "slides": slides,
            }
        )
    )
    return path


def entry(number, title, part, excerpt):
    return {
        "structure": str(number),
        "title": title,
        "title_origin": "generated",
        "physical_index": number,
        "anchor": {"unit": number, "part": part, "range": [0, len(excerpt)], "excerpt": excerpt},
    }


def model_responses(monkeypatch, responses):
    import litellm

    answers = iter(responses)
    previous = litellm.completion

    def completion(**kwargs):
        response = previous(**kwargs)
        if "PHYSICAL SLIDE STRUCTURE" in str(kwargs["messages"]):
            response.choices[0].message = SimpleNamespace(content=json.dumps(next(answers)))
        return response

    async def acompletion(**kwargs):
        return completion(**kwargs)

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(litellm, "acompletion", acompletion)


def read_index(tmp_path, package, **options):
    from pageindex import IndexConfig
    from pageindex.storage.sqlite import SQLiteStorage

    from openkb.block_package import create_index_client

    store = tmp_path / "index"
    with SQLiteStorage(str(store / "pageindex.db")) as storage:
        collection = create_index_client(
            storage_path=str(store),
            storage=storage,
            model="gpt-4o",
            index_config=IndexConfig(
                if_add_node_summary=False, if_add_doc_description=False, **options
            ),
        ).collection()
        return collection.get_document(collection.add(str(package)))


def test_image_cover_preface_anchors_original_speaker_notes(tmp_path, pdf_model, monkeypatch):
    package = slide_package(tmp_path, ["", "Bravo"], ["Cover speaker note", ""], cover_image=True)
    model_responses(monkeypatch, [[entry(2, "Main section", "body", "Bravo")]])
    result = read_index(tmp_path, package)
    anchor = result["structure"][0]["anchor"]
    assert anchor == {"unit": 1, "part": "notes", "range": [0, 18], "excerpt": "Cover speaker note"}


def test_recursive_slide_tree_preserves_physical_page_kind(tmp_path, pdf_model, monkeypatch):
    package = slide_package(tmp_path, ["Alpha", "Bravo", "Charlie"], ["", "", ""])
    root = entry(1, "Root", "body", "Alpha")
    model_responses(
        monkeypatch,
        [[root], [root, entry(2, "Second", "body", "Bravo"), entry(3, "Third", "body", "Charlie")]],
    )
    result = read_index(tmp_path, package, max_page_num_each_node=0, max_token_num_each_node=1)
    children = result["structure"][0]["nodes"]
    assert [item["start_index"] for item in children] == [2, 3]
    assert [item["unit_kind"] for item in children] == ["page", "page"]
