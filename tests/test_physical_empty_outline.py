"""An empty heading proposal can retain a document range, never heading proof."""

import pytest
from pageindex.errors import IndexQualityError
from pageindex.index.physical_sections import SectionBoundary, prepare_entries
from pageindex.index.utils import add_node_text, post_processing


def test_empty_root_outline_keeps_every_original_page_without_inventing_a_heading():
    from pageindex.index.physical_sections import document_range_entry

    pages = [("", 0), ("The room permits quiet reading.\n", 10), ("Keep this final page.\n", 7)]
    assert prepare_entries([], pages) == []
    [node] = post_processing([document_range_entry(pages)], len(pages))
    assert (node["start_index"], node["end_index"]) == (1, 3)
    assert node["title_origin"] == "generated"
    assert "anchor" not in node and "end_anchor" not in node
    assert node["source_node_id"]
    add_node_text(node, pages)
    assert node["text"] == "".join(page[0] for page in pages)
    assert not SectionBoundary.from_node(node).start_confirmed


def test_empty_subtree_does_not_become_whole_document_navigation():
    with pytest.raises(IndexQualityError):
        prepare_entries(
            [],
            [("Original body.", 4)],
            start_index=7,
            boundary=SectionBoundary("Parent", (7, 0), (8, 0)),
        )


@pytest.mark.parametrize("entries", [None, {}, [None], [{"physical_index": 1}]])
def test_malformed_outline_is_still_rejected(entries):
    with pytest.raises(IndexQualityError):
        prepare_entries(entries, [("Original body.", 4)])


@pytest.mark.parametrize("pages", [[], [(" \n", 0)]])
def test_range_navigation_requires_actual_source_text(pages):
    from pageindex.index.physical_sections import document_range_entry

    with pytest.raises(IndexQualityError):
        document_range_entry(pages)
