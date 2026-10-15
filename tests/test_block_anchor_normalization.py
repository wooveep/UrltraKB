"""Only unique verbatim original quotes can repair block coordinates."""

from copy import deepcopy

import pytest
from pageindex.index.block_policy import BlockPolicy


def policy(text="# old comment\n\nStored original body.\n", *, generated=()):
    return BlockPolicy(
        {
            "unit_count": 1,
            "source": {
                "text": text,
                "blocks": [
                    {
                        "ordinal": 1,
                        "source_spans": [[0, len(text)]],
                        "headings": [{"title": "old comment", "source_span": [0, 14]}],
                    }
                ],
                "origins": [
                    {"kind": "generated", "normalized_span": list(span)} for span in generated
                ],
            },
        }
    )


def entry():
    return {
        "structure": "1",
        "title": "old comment",
        "title_origin": "original",
        "physical_index": 1,
        "anchor": {
            "unit": 1,
            "part": "body",
            "range": [0, 14],
            "excerpt": "# old comment",
        },
    }


def test_unique_original_heading_repairs_only_its_coordinates():
    original = entry()
    expected = deepcopy(original)
    expected["anchor"]["range"] = [0, 13]
    boundary = policy()
    assert boundary.validate_entry(original) == "excerpt_mismatch"
    assert boundary.normalize_anchor(original)
    assert original == expected
    assert boundary.valid(original)
    assert not boundary.normalize_anchor(original)


@pytest.mark.parametrize(
    "field,value",
    [
        ("unit", 2),
        ("part", "notes"),
        ("range", [-1, 14]),
        ("range", [False, 14]),
        ("excerpt", "invented"),
    ],
)
def test_normalization_never_guesses_attribution_or_original_text(field, value):
    original = entry()
    original["anchor"][field] = value
    before = deepcopy(original)
    assert not policy().normalize_anchor(original)
    assert original == before


def test_ambiguous_quote_cannot_establish_original_coordinates():
    original = entry()
    before = deepcopy(original)
    assert not policy("# old comment\n# old comment\n").normalize_anchor(original)
    assert original == before


def test_generated_formatting_cannot_repair_original_evidence():
    original = entry()
    before = deepcopy(original)
    assert not policy(generated=[(0, 13)]).normalize_anchor(original)
    assert original == before


def test_coordinate_repair_does_not_bypass_declared_heading_contract():
    original = entry()
    original["title"] = "undeclared heading"
    before = deepcopy(original)
    assert not policy().normalize_anchor(original)
    assert original == before


def test_pipeline_normalizes_unique_anchors_before_title_verification():
    original = entry()
    boundary = policy()
    boundary.normalize_title_origin(original)
    boundary.validate_order([original])
    assert original["title_origin"] == "original"
    assert original["anchor"]["range"] == [0, 13]
