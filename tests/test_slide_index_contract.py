"""Frozen slide inputs keep valid original anchors throughout shared tree recursion."""

import base64
import hashlib
import json
from types import SimpleNamespace

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
        identity = collection.add(str(package))
        result = collection.get_document(identity)
        result["pages"] = collection.get_page_content(identity, f"1-{result['page_count']}")
        return result


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


@pytest.mark.parametrize("bodies", [["Body", ""], ["", "Body"], ["", "Body", "", "", "End", ""]])
def test_textless_slide_navigation_needs_no_correction_and_keeps_pages(
    tmp_path, pdf_model, monkeypatch, bodies
):
    package = slide_package(tmp_path, bodies, [""] * len(bodies), cover_image=True)
    generated = [entry(i, body or "Image page", "body", body) for i, body in enumerate(bodies, 1)]
    model_responses(monkeypatch, [generated])
    calls = []
    from litellm import acompletion

    async def record(**kwargs):
        calls.append(kwargs)
        return await acompletion(**kwargs)

    monkeypatch.setattr("litellm.acompletion", record)
    result = read_index(tmp_path, package)
    tree = result["structure"]
    assert len(result["pages"]) == len(bodies)
    assert [item["anchor"]["unit"] for item in tree] == [
        i for i, body in enumerate(bodies, 1) if body
    ]
    assert tree[0]["start_index"] == 1
    assert tree[-1]["end_index"] == len(bodies)
    assert all(item["anchor"]["excerpt"] for item in tree)
    assert result["pages"][0]["images"]
    assert calls == []


def test_only_textless_labels_get_a_root_anchored_in_actual_notes(tmp_path, pdf_model, monkeypatch):
    package = slide_package(tmp_path, ["", "", ""], ["", "Actual note", ""], cover_image=True)
    model_responses(monkeypatch, [[entry(1, "Image cover", "body", "")]])
    result = read_index(tmp_path, package)
    root = result["structure"][0]
    assert root["start_index"] == 1 and root["end_index"] == 3
    assert root["anchor"] == {
        "unit": 2,
        "part": "notes",
        "range": [0, 11],
        "excerpt": "Actual note",
    }
    assert len(result["pages"]) == 3


def test_textless_parent_keeps_its_valid_descendant(tmp_path, pdf_model, monkeypatch):
    package = slide_package(tmp_path, ["", "Body"], ["", ""], cover_image=True)
    child = entry(2, "Child", "body", "Body")
    child["structure"] = "1.1"
    model_responses(monkeypatch, [[entry(1, "Image parent", "body", ""), child]])
    root = read_index(tmp_path, package)["structure"][0]
    assert root["title"] == "Child"
    assert root["start_index"] == 1 and root["end_index"] == 2
    assert root["anchor"]["unit"] == 2


def test_entirely_textless_slides_fail_before_any_model_request(tmp_path, pdf_model, monkeypatch):
    from pageindex.index.page_parts_policy import PagePartsContractError

    package = slide_package(tmp_path, ["", " "], ["", ""], cover_image=True)
    original = package.read_bytes()

    def unexpected(**kwargs):
        pytest.fail("Textless slides cannot request a text navigation tree")

    monkeypatch.setattr("litellm.completion", unexpected)
    monkeypatch.setattr("litellm.acompletion", unexpected)
    with pytest.raises(Exception, match="No content is available for text navigation") as failure:
        read_index(tmp_path, package)
    assert isinstance(failure.value, PagePartsContractError)
    assert package.read_bytes() == original


@pytest.mark.parametrize("bad_field", ["excerpt", "range", "unit", "title_origin"])
def test_malformed_image_page_anchors_are_repaired_instead_of_silently_removed(
    tmp_path, pdf_model, monkeypatch, bad_field
):
    package = slide_package(tmp_path, ["", "Body"], ["", ""], cover_image=True)
    invalid = entry(1, "Image", "body", "")
    if bad_field == "title_origin":
        invalid[bad_field] = "original"
    else:
        invalid["anchor"][bad_field] = {"excerpt": "invented", "range": [0, 9], "unit": 2}[
            bad_field
        ]
    model_responses(monkeypatch, [[invalid], [entry(2, "Real body", "body", "Body")]])
    from litellm import acompletion

    corrections = []

    async def repair(**kwargs):
        corrections.append(kwargs)
        return await acompletion(**kwargs)

    monkeypatch.setattr("litellm.acompletion", repair)
    result = read_index(tmp_path, package)
    assert corrections
    assert result["structure"][0]["anchor"] == entry(2, "", "body", "Body")["anchor"]
    assert len(result["pages"]) == 2


def test_navigation_policy_separates_new_trees_and_keeps_old_cache_readable(
    tmp_path, pdf_model, monkeypatch
):
    from pageindex import IndexConfig
    from pageindex.index.page_parts_policy import PAGE_PARTS_INDEX_POLICY
    from pageindex.storage.sqlite import SQLiteStorage

    from openkb.block_package import create_index_client
    from openkb.office.slide_package import FrozenSlideParser

    package = slide_package(tmp_path, ["", "Body"], ["", ""], cover_image=True)
    assert PAGE_PARTS_INDEX_POLICY in FrozenSlideParser.policy
    model_responses(monkeypatch, [[entry(2, "Root", "body", "Body")]] * 2)
    store = tmp_path / "index"
    with SQLiteStorage(str(store / "pageindex.db")) as storage:
        collection = create_index_client(
            storage_path=str(store),
            storage=storage,
            model="gpt-4o",
            index_config=IndexConfig(if_add_node_summary=False, if_add_doc_description=False),
        ).collection()
        first = collection.add(str(package))
        before = collection.get_document(first)
        monkeypatch.setattr(FrozenSlideParser, "policy", FrozenSlideParser.policy + ":fixture-next")
        assert collection.get_document(first) == before
        assert collection.get_page_content(first, "1")[0]["images"]
        assert collection.add(str(package)) != first
        assert collection.get_document(first) == before
