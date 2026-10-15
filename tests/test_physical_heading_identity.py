"""Native section identity survives same-title parents and children."""

import pytest
from pageindex.errors import IndexQualityError
from pageindex.index.physical_sections import prepare_entries


def index_headings(text, entries):
    return prepare_entries(entries, [(text, 40)])


def test_same_title_parent_and_child_keep_their_native_section_positions():
    text = "20.常用其它命令\n20.1.\n常用qemu 命令\n20.2.\n常用其它命令\n"
    result = index_headings(
        text,
        [
            {"title": title, "structure": section, "physical_index": "<physical_index_1>"}
            for title, section in [
                ("常用其它命令", "20"),
                ("常用qemu 命令", "20.1"),
                ("常用其它命令", "20.2"),
            ]
        ],
    )
    assert [item["structure"] for item in result] == ["20", "20.1", "20.2"]
    assert [item["anchor"]["excerpt"] for item in result] == [
        "20.常用其它命令",
        "20.1.\n常用qemu 命令",
        "20.2.\n常用其它命令",
    ]
    assert [item["anchor"]["range"][0] for item in result] == [0, 10, 26]


@pytest.mark.parametrize(
    ("text", "title", "structure"),
    [
        ("20.2.\nCommands\n", "Commands", "20"),
        ("20.2.Commands\n", "20.2.Commands", "20"),
        ("20.Commands\n20.Commands\n", "Commands", "20"),
        ("1.Commands\n2.Commands\n", "Commands", None),
        ("3.14\n", "14", "3"),
    ],
)
def test_ambiguous_or_conflicting_identity_cannot_supply_a_native_anchor(text, title, structure):
    result = index_headings(
        text,
        [{"title": title, "structure": structure, "physical_index": "<physical_index_1>"}],
    )
    # A delivered title is no proof of a character offset or permission to
    # replace its section number with a different same-title node.
    assert result[0]["structure"] == structure
    assert "anchor" not in result[0]


@pytest.mark.parametrize("structure", [[20], {"number": 20}, 20])
def test_invalid_model_section_identity_is_a_recoverable_quality_error(structure):
    with pytest.raises(IndexQualityError, match="structure must be a string or null"):
        index_headings(
            "20.Commands\n",
            [{"title": "Commands", "structure": structure, "physical_index": "<physical_index_1>"}],
        )
