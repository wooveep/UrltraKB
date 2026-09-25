"""Frozen source coordinates remain distinct from short transport identities."""

import json

from openkb.agent.source_protocol import source_messages


def _evidence(text="The literal marker @e:a is source data."):
    return {
        "group_id": "a" * 64,
        "source_id": "b" * 64,
        "version_id": "c" * 64,
        "parse_id": "d" * 64,
        "blocks": [
            {"id": "e" * 64, "order": 7, "text": text},
            {
                "id": "f" * 64,
                "order": 9,
                "text": text[:8],
                "reference": {"start": 4, "end": 12},
            },
        ],
    }


def test_navigation_and_planning_share_frozen_non_numeric_ids_and_exact_extents():
    evidence = _evidence()
    navigation = source_messages(evidence, {"stage": "index_structure"}, "Navigate")
    planning = source_messages(evidence, {"stage": "planning"}, "Plan")
    nav = json.loads(navigation[-1]["content"])
    plan = json.loads(planning[-1]["content"])

    assert nav["protocol"] == plan["protocol"] == "source-prefix-v3"
    assert (
        navigation[-1]["content"].split(',"stage":', 1)[0]
        == planning[-1]["content"].split(',"stage":', 1)[0]
    )
    assert nav["evidence"] == plan["evidence"]
    assert nav["identity_protocol"] == plan["identity_protocol"]
    assert nav["evidence"]["blocks"][0]["block_range"] == [7, 8]
    assert nav["evidence"]["blocks"][0]["text_extent"] == {
        "start_char": 0,
        "end_char": len(evidence["blocks"][0]["text"]),
        "complete_block": True,
    }
    assert nav["evidence"]["blocks"][1]["block_range"] == [9, 10]
    assert nav["evidence"]["blocks"][1]["text_extent"] == {
        "start_char": 4,
        "end_char": 12,
        "complete_block": False,
    }
    ids = [block["id"] for block in nav["evidence"]["blocks"]]
    assert ids[0] != ids[1]
    assert all(not any(character.isdigit() for character in identity) for identity in ids)
    assert all(nav["identity_protocol"]["namespace"] in identity for identity in ids)
    assert nav["identity_protocol"]["namespace"] not in evidence["blocks"][0]["text"]


def test_source_identity_roundtrip_does_not_replace_literal_source_text():
    evidence = _evidence()
    request = source_messages(evidence, {"stage": "planning"}, "Plan")
    block = json.loads(request[-1]["content"])["evidence"]["blocks"][0]
    assert "@e:a" in block["text"]
    response = {"page_changes": [{"name": "concepts/example", "title": "@e:a"}]}
    assert (
        json.loads(request.decode_response(json.dumps(response)))["page_changes"][0]["title"]
        == "@e:a"
    )


def test_repeated_slices_of_one_block_keep_distinct_exact_character_extents():
    evidence = _evidence("A source block with two overlapping slices.")
    whole = evidence["blocks"][0]
    evidence["blocks"] = [
        {**whole, "text": whole["text"][4:16], "reference": {"start": 4, "end": 16}},
        {**whole, "text": whole["text"][12:24], "reference": {"start": 12, "end": 24}},
    ]
    request = source_messages(evidence, {"stage": "planning"}, "Plan")
    blocks = json.loads(request[-1]["content"])["evidence"]["blocks"]
    assert blocks[0]["id"] == blocks[1]["id"]
    assert blocks[0]["block_range"] == blocks[1]["block_range"] == [7, 8]
    assert blocks[0]["text_extent"] != blocks[1]["text_extent"]
    assert all(not block["text_extent"]["complete_block"] for block in blocks)
