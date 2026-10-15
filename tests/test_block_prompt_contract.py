"""Block prompts provide legal native anchors and the parser's correction envelope."""

import json

import pytest
from pageindex.index.block_policy import BlockPolicy
from test_source_qualification import freeze_source

from ultrakb.block_package import FrozenBlockParser, freeze_block_package


@pytest.fixture
def heading_policy(kb_dir, tmp_path):
    source, _ = freeze_source(
        kb_dir, tmp_path, "md", "# Title\n\nFirst condition.\nSecond condition.\n"
    )
    package = freeze_block_package(source, "manual")
    parsed = FrozenBlockParser().parse(str(package), images_dir=str(tmp_path / "images"))
    return BlockPolicy(parsed.metadata)


def original_entry(candidate, *, title="Title", unit=1):
    return {
        "structure": "1",
        "title": title,
        "title_origin": "original",
        "physical_index": unit,
        "anchor": {
            "unit": unit,
            "part": "body",
            "range": candidate["range"],
            "excerpt": candidate["excerpt"],
        },
    }


def test_heading_with_body_has_a_legal_original_evidence_candidate(heading_policy):
    policy = heading_policy
    before = json.dumps(policy.units, sort_keys=True)
    catalog = json.loads(policy.render(1))
    heading = catalog["headings"][0]
    candidates = [
        item
        for item in catalog["original_evidence"]
        if item.get("heading_title") == heading["title"]
    ]
    assert candidates
    for candidate in candidates:
        start, end = candidate["range"]
        assert heading["source_span"][0] <= start < end <= heading["source_span"][1]
        assert candidate["excerpt"] == policy.text[start:end]
        assert policy.validate_entry(original_entry(candidate)) is None
    body = next(item for item in catalog["original_evidence"] if "heading_title" not in item)
    assert policy.validate_entry(original_entry(body)) == "original_heading_anchor_mismatch"
    assert json.dumps(policy.units, sort_keys=True) == before


def test_heading_candidate_uses_only_original_slices_outside_generated_formatting():
    text = "## Title\nBody.\n"
    policy = BlockPolicy(
        {
            "unit_count": 1,
            "source": {
                "text": text,
                "blocks": [
                    {
                        "ordinal": 1,
                        "source_spans": [[0, len(text)]],
                        "headings": [{"title": "Title", "source_span": [0, 9]}],
                    }
                ],
                "origins": [
                    {"kind": "generated", "normalized_span": [0, 3]},
                    {"kind": "identity", "normalized_span": [3, len(text)]},
                ],
            },
        }
    )
    candidates = [
        item
        for item in json.loads(policy.render(1))["original_evidence"]
        if "heading_title" in item
    ]
    assert candidates == [{"range": [3, 9], "excerpt": "Title\n", "heading_title": "Title"}]
    assert policy.validate_entry(original_entry(candidates[0])) is None


def test_heading_candidate_cannot_borrow_another_blocks_original_evidence():
    text = "First.\n# Title\nBody.\n"
    policy = BlockPolicy(
        {
            "unit_count": 2,
            "source": {
                "text": text,
                "blocks": [
                    {
                        "ordinal": 1,
                        "source_spans": [[0, 7]],
                        "headings": [{"title": "Title", "source_span": [7, 15]}],
                    },
                    {
                        "ordinal": 2,
                        "source_spans": [[7, len(text)]],
                        "headings": [{"title": "Title", "source_span": [7, 15]}],
                    },
                ],
                "origins": [{"kind": "identity", "normalized_span": [0, len(text)]}],
            },
        }
    )
    assert not any(
        "heading_title" in item for item in json.loads(policy.render(1))["original_evidence"]
    )
    candidates = [
        item
        for item in json.loads(policy.render(2))["original_evidence"]
        if "heading_title" in item
    ]
    assert candidates == [{"range": [7, 15], "excerpt": "# Title\n", "heading_title": "Title"}]
    assert policy.validate_entry(original_entry(candidates[0], unit=2)) is None


def test_correction_prompt_matches_the_strict_one_entry_array_parser(heading_policy):
    policy = heading_policy
    catalog = json.loads(policy.render(1))
    candidate = next(item for item in catalog["original_evidence"] if "heading_title" in item)
    corrected = original_entry(candidate)
    bad = original_entry(
        next(item for item in catalog["original_evidence"] if "heading_title" not in item)
    )
    prompt = policy.correction_prompt(bad, 1, 1)
    assert "JSON array containing exactly one corrected entry" in prompt
    assert "A bare JSON object, an empty array, or multiple entries are invalid" in prompt
    assert "original_heading_anchor_mismatch" in prompt
    assert json.dumps(candidate, ensure_ascii=False) in prompt
    assert policy.parse_correction(corrected, 1, 1) is None
    assert policy.parse_correction([], 1, 1) is None
    assert policy.parse_correction([corrected, corrected], 1, 1) is None
    assert policy.parse_correction([corrected], 1, 1) == {
        key: corrected[key] for key in ("title", "title_origin", "physical_index", "anchor")
    }
