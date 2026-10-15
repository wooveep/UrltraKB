"""Frozen slide inputs keep valid original anchors throughout shared tree recursion."""

import base64
import hashlib
import json

import pytest

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
                "format": "ultrakb-slide-pdf-v1",
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


def policy_for(bodies, notes):
    from pageindex.index.page_parts_policy import PagePartsPolicy

    return PagePartsPolicy(
        {
            "unit_count": len(bodies),
            "page_parts": [
                {"body": body + ("\n" if body else ""), "notes": note}
                for body, note in zip(bodies, notes)
            ],
        }
    )


def render_tree(boundary, entries, stop):
    from pageindex.index.utils import post_processing

    entries = boundary.prepare_navigation(entries, 1, stop)
    entries = boundary.preface(entries)
    for item in entries:
        item["appear_start"] = boundary.starts_unit(item)
    return post_processing(entries, stop, policy=boundary)


def test_image_cover_preface_anchors_original_speaker_notes(tmp_path):
    package = slide_package(tmp_path, ["", "Bravo"], ["Cover speaker note", ""], cover_image=True)
    boundary = policy_for(["", "Bravo"], ["Cover speaker note", ""])
    tree = render_tree(boundary, [entry(2, "Main section", "body", "Bravo")], 2)
    assert tree[0]["anchor"] == {
        "unit": 1,
        "part": "notes",
        "range": [0, 18],
        "excerpt": "Cover speaker note",
    }
    from ultrakb.office.slide_package import FrozenSlideParser

    parsed = FrozenSlideParser().parse(str(package), images_dir=str(tmp_path / "images"))
    assert parsed.nodes[0].images
    assert parsed.nodes[0].metadata["parts"]["notes"] == "Cover speaker note"


def test_recursive_slide_tree_preserves_physical_page_kind():
    boundary = policy_for(["Alpha", "Bravo", "Charlie"], ["", "", ""])
    root = entry(1, "Root", "body", "Alpha")
    second = entry(2, "Second", "body", "Bravo")
    third = entry(3, "Third", "body", "Charlie")
    second["structure"], third["structure"] = "1.1", "1.2"
    children = render_tree(boundary, [root, second, third], 3)[0]["nodes"]
    assert [item["start_index"] for item in children] == [2, 3]
    assert [item["unit_kind"] for item in children] == ["page", "page"]


@pytest.mark.parametrize("bodies", [["Body", ""], ["", "Body"], ["", "Body", "", "", "End", ""]])
def test_textless_slide_navigation_needs_no_correction_and_keeps_pages(tmp_path, bodies):
    from ultrakb.office.slide_package import FrozenSlideParser

    package = slide_package(tmp_path, bodies, [""] * len(bodies), cover_image=True)
    boundary = policy_for(bodies, [""] * len(bodies))
    entries = [entry(i, body or "Image page", "body", body) for i, body in enumerate(bodies, 1)]
    tree = render_tree(boundary, entries, len(bodies))
    parsed = FrozenSlideParser().parse(str(package), images_dir=str(tmp_path / "images"))
    assert len(parsed.nodes) == len(bodies)
    assert [item["anchor"]["unit"] for item in tree] == [
        i for i, body in enumerate(bodies, 1) if body
    ]
    assert tree[0]["start_index"] == 1
    assert tree[-1]["end_index"] == len(bodies)
    assert all(item["anchor"]["excerpt"] for item in tree)
    assert parsed.nodes[0].images


def test_only_textless_labels_get_a_root_anchored_in_actual_notes():
    boundary = policy_for(["", "", ""], ["", "Actual note", ""])
    root = render_tree(boundary, [entry(1, "Image cover", "body", "")], 3)[0]
    assert root["start_index"] == 1 and root["end_index"] == 3
    assert root["anchor"] == {
        "unit": 2,
        "part": "notes",
        "range": [0, 11],
        "excerpt": "Actual note",
    }


def test_textless_parent_keeps_its_valid_descendant():
    boundary = policy_for(["", "Body"], ["", ""])
    child = entry(2, "Child", "body", "Body")
    child["structure"] = "1.1"
    root = render_tree(boundary, [entry(1, "Image parent", "body", ""), child], 2)[0]
    assert root["title"] == "Child"
    assert root["start_index"] == 1 and root["end_index"] == 2
    assert root["anchor"]["unit"] == 2


def test_entirely_textless_slides_fail_before_any_model_request(tmp_path, monkeypatch):
    from pageindex import IndexConfig
    from pageindex.index.page_parts_policy import PagePartsContractError

    from ultrakb.index_client import create_index_client

    package = slide_package(tmp_path, ["", " "], ["", ""], cover_image=True)
    original = package.read_bytes()

    def unexpected(*args, **kwargs):
        pytest.fail("Textless slides cannot request a text navigation tree")

    monkeypatch.setattr("pageindex.default_llm.DefaultIndexLLM.complete", unexpected)
    monkeypatch.setattr("pageindex.default_llm.DefaultIndexLLM.acomplete", unexpected)
    with create_index_client(
        storage_path=str(tmp_path / "index"),
        index_config=IndexConfig(if_add_node_summary=False, if_add_doc_description=False),
    ) as client:
        with pytest.raises(
            PagePartsContractError, match="No content is available for text navigation"
        ):
            client.collection().add(str(package))
    assert package.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_field", ["excerpt", "range", "unit", "title_origin"])
async def test_malformed_image_page_anchors_are_repaired_instead_of_silently_removed(
    live_llm, bad_field
):
    from pageindex.index.utils import extract_json
    from test_index_llm import RecordingIndexLLM

    boundary = policy_for(["", "Body"], ["", ""])
    invalid = entry(1, "Image", "body", "")
    if bad_field == "title_origin":
        invalid[bad_field] = "original"
    else:
        invalid["anchor"][bad_field] = {"excerpt": "invented", "range": [0, 9], "unit": 2}[
            bad_field
        ]
    assert not boundary.valid(invalid)
    with pytest.raises(ValueError, match="Unverified"):
        boundary.validate_order([invalid])
    runtime = RecordingIndexLLM(live_llm=live_llm)

    async def complete(*, model, prompt):
        return (
            await runtime.acomplete([{"role": "user", "content": prompt}], stage="correction")
        ).text

    fixed = await boundary.fix(invalid, 1, 2, "deepseek/deepseek-flash", complete, extract_json)
    assert fixed is not None
    corrected = {**invalid, **fixed}
    assert boundary.valid(corrected)
    anchor = corrected["anchor"]
    assert anchor["unit"] == 2 and anchor["part"] == "body"
    left, right = anchor["range"]
    assert boundary.parts[1]["body"][left:right] == anchor["excerpt"]
    assert anchor["excerpt"].strip() == "Body"
    assert [stage for stage, _ in runtime.calls] == ["correction"]


def test_navigation_policy_separates_new_trees_and_keeps_old_cache_readable(
    tmp_path, live_llm, monkeypatch
):
    from pageindex import IndexConfig
    from pageindex.index.page_parts_policy import PAGE_PARTS_INDEX_POLICY
    from test_index_llm import RecordingIndexLLM

    from ultrakb.index_client import create_index_client
    from ultrakb.office.slide_package import FrozenSlideParser

    package = slide_package(tmp_path, ["", "Body"], ["", ""], cover_image=True)
    assert PAGE_PARTS_INDEX_POLICY in FrozenSlideParser.policy
    runtime = RecordingIndexLLM(live_llm=live_llm)
    with create_index_client(
        storage_path=str(tmp_path / "index"),
        model="deepseek/deepseek-flash",
        index_config=IndexConfig(
            llm_client=runtime,
            require_llm_client=True,
            if_add_node_summary=False,
            if_add_doc_description=False,
        ),
    ) as client:
        collection = client.collection()
        first = collection.add(str(package))
        before = collection.get_document(first)
        monkeypatch.setattr(FrozenSlideParser, "policy", FrozenSlideParser.policy + ":fixture-next")
        assert collection.get_document(first) == before
        assert collection.get_page_content(first, "1")[0]["images"]
        assert collection.add(str(package)) != first
        assert collection.get_document(first) == before
