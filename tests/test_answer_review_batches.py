"""Review transport failures and bounded units through the public conversation seam."""

import json

import pytest

from openkb.application.conversations import continue_conversation
from openkb.locks import atomic_write_text
from tests.http_model_fixture import answer_review_response


def _reader(answer):
    def chat(body):
        if any(row["role"] == "tool" for row in body["messages"]):
            return {"role": "assistant", "content": answer}
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "read",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"sources/rows.md"}'},
                }
            ],
        }

    return chat


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_bounded_reviews_keep_whole_question_evidence_and_missing_rows(
    kb_dir, model_service, missing
):
    target = "sources/snapshots/v-p.md#block-rows"
    rows = [
        f"Channel {i}: {7400 + i}; {'standby only' if i == 4 else 'normal'}." for i in range(1, 13)
    ]
    answer = "\n".join(f"{row} [Source]({target})" for row in rows[: 11 if missing else 12])
    source = "\n".join(rows) + f"\n[Source]({target})"
    atomic_write_text(kb_dir / "wiki/sources/rows.md", source)
    reviews, edits = [], []
    reader = _reader(answer)

    def chat(body):
        raw = body["messages"][-1]["content"]
        if '"stage": "answer_correction"' in raw:
            payload = json.loads(raw)
            edits.append(payload)
            return {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "insertions": [
                            {
                                "after": payload["units"][-1]["id"],
                                "text": f"\n{rows[-1]} [Source]({target})",
                            }
                        ]
                    }
                ),
            }
        return reader(body)

    def review(body):
        payload = json.loads(body["messages"][-1]["content"])
        reviews.append(payload)
        # Reproduce an output that cannot finish a large per-unit review.
        model_service.finish_reason = "length" if len(payload["units"]) > 8 else "stop"
        value = answer_review_response(payload)
        if missing and rows[-1] not in payload["answer"]:
            value.update(
                verdict="unsupported",
                issues=[
                    {
                        "kind": "missing",
                        "units": [],
                        "claim": "",
                        "reason": "Channel 12 is requested and present in the original rows.",
                    }
                ],
            )
        assert payload["question"] == "List every channel, value and condition."
        assert source in str(payload["observations"]).replace("\\n", "\n")
        assert rows[0] in payload["answer"] and rows[10] in payload["answer"]
        return {"role": "assistant", "content": json.dumps(value)}

    model_service.chat_response = chat
    model_service.chat_without_tools = True
    model_service.answer_review_response = review
    result = await continue_conversation(kb_dir, "List every channel, value and condition.")
    assert result.status == "completed", result
    assert all(row in result.answer for row in rows)
    assert len(reviews) > 1 and all(len(p["units"]) <= 8 for p in reviews)
    assert bool(edits) == missing
    assert all(not p["editable_units"] and p["insertions_allowed"] for p in edits)
    for candidate in {p["answer"] for p in reviews}:
        reviewed = [u["id"] for p in reviews if p["answer"] == candidate for u in p["units"]]
        assert len(reviewed) == len(set(reviewed))
    assert result.usage["observable_attempts"] == len(model_service)


@pytest.mark.asyncio
async def test_invalid_protocol_does_not_spend_the_semantic_correction(kb_dir, model_service):
    target = "sources/snapshots/v-p.md#block-rows"
    bad = f"Channel 4: normal [Source]({target})"
    good = f"Channel 4: standby only [Source]({target})"
    atomic_write_text(kb_dir / "wiki/sources/rows.md", good)
    reviews, corrections = [], []
    reader = _reader(bad)

    def chat(body):
        raw = body["messages"][-1]["content"]
        if '"stage": "answer_correction"' in raw:
            payload = json.loads(raw)
            corrections.append(payload)
            return {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "edits": [
                            {
                                "unit": payload["editable_units"][0],
                                "text": good,
                            }
                        ]
                    }
                ),
            }
        return reader(body)

    def review(body):
        payload = json.loads(body["messages"][-1]["content"])
        reviews.append(payload)
        if len(reviews) == 1:
            return {"role": "assistant", "content": '{"verdict":"supported","units":'}
        value = answer_review_response(payload)
        if payload["answer"] == bad:
            value["units"][0]["verdict"] = "unsupported"
            value.update(
                verdict="unsupported",
                issues=[
                    {
                        "kind": "unsupported",
                        "units": [payload["units"][0]["id"]],
                        "claim": "normal",
                        "reason": "Channel 4 is standby only.",
                    }
                ],
            )
        return {"role": "assistant", "content": json.dumps(value)}

    model_service.chat_response = chat
    model_service.chat_without_tools = True
    model_service.answer_review_response = review
    result = await continue_conversation(kb_dir, "What is channel 4's condition?")
    assert result.status == "completed" and result.answer == good, result
    assert [p["answer"] for p in reviews] == [bad, bad, good]
    assert len(corrections) == 1
    assert result.usage["observable_attempts"] == 6


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    [
        None,
        "array",
        "array_number_form",
        "array_wrong_type",
        "wrong_type",
        "wrong_path",
        "partial_object",
        "quote_and_path",
    ],
)
async def test_metadata_support_binds_an_exact_typed_value(kb_dir, model_service, damage):
    metadata = {"coverage": {"complete": False}, "blocks": [{"characters": 42}]}
    atomic_write_text(kb_dir / "wiki/sources/rows.md", json.dumps(metadata))
    model_service.chat_response = _reader("Source coverage is partial.")
    model_service.chat_without_tools = True
    reviews = []

    def review(body):
        payload = json.loads(body["messages"][-1]["content"])
        reviews.append(payload)
        assert payload["observations"][0]["output"] == metadata
        support = {"observation": "o1", "path": ["coverage", "complete"], "value": False}
        if damage == "wrong_type":
            support["value"] = 0
        elif damage == "wrong_path":
            support["path"] = ["complete"]
        elif damage == "array":
            support.update(path=["blocks"], value=metadata["blocks"])
        elif damage == "array_number_form":
            support.update(path=["blocks"], value=[{"characters": 42.0}])
        elif damage == "array_wrong_type":
            support.update(path=["blocks"], value=[{"characters": "42"}])
        elif damage == "partial_object":
            support.update(path=[], value={"coverage": metadata["coverage"]})
        elif damage == "quote_and_path":
            support["quote"] = "anything"
        value = answer_review_response(payload)
        value["units"][0]["support"] = [support]
        return {"role": "assistant", "content": json.dumps(value)}

    model_service.answer_review_response = review
    result = await continue_conversation(kb_dir, "Is source coverage complete?")
    valid = damage in {None, "array", "array_number_form"}
    assert (result.status == "completed") == valid, result
    assert len(reviews) == (1 if valid else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    [
        None,
        "changed_coordinate",
        "boolean",
        "string_index",
        "large_rounded",
        "rounded_wire_integer",
        "rounded_wire_fraction",
        "nonfinite",
    ],
)
async def test_coordinate_support_preserves_exact_numbers_across_json_number_forms(
    kb_dir, model_service, damage
):
    metadata = {"figures": [{"bbox": [115.0, 261.5, 228.5, 278.5]}]}
    claimed = 115
    if damage == "large_rounded":
        metadata["figures"][0]["bbox"][0] = 9007199254740993
        claimed = 9007199254740992.0
    elif damage == "nonfinite":
        metadata["figures"][0]["bbox"][0] = claimed = float("inf")
    elif damage == "rounded_wire_integer":
        metadata["figures"][0]["bbox"][0] = 9007199254740992
    answer = f"The figure starts at x={claimed}."
    atomic_write_text(kb_dir / "wiki/sources/rows.md", json.dumps(metadata))
    model_service.chat_response = _reader(answer)
    model_service.chat_without_tools = True

    def review(body):
        payload = json.loads(body["messages"][-1]["content"])
        support = {
            "observation": "o1",
            "path": ["figures", 0, "bbox"],
            "value": [claimed, 261.5, 228.5, 278.5],
        }
        if damage == "changed_coordinate":
            support["value"][0] = 115.00001
        elif damage == "boolean":
            support["value"][0] = True
        elif damage == "string_index":
            support["path"][1] = "0"
        value = answer_review_response(payload)
        value["units"][0]["support"] = [support]
        if damage in {"rounded_wire_integer", "rounded_wire_fraction"}:
            support["value"][0] = "WIRE_NUMBER"
            literal = (
                "9007199254740993.0"
                if damage == "rounded_wire_integer"
                else "115.00000000000000001"
            )
            return {
                "role": "assistant",
                "content": json.dumps(value).replace('"WIRE_NUMBER"', literal),
            }
        return {"role": "assistant", "content": json.dumps(value)}

    model_service.answer_review_response = review
    result = await continue_conversation(kb_dir, "Where does the figure start?")
    assert (result.status == "completed") == (damage is None), result


@pytest.mark.asyncio
@pytest.mark.parametrize("invented_closing_brace", [False, True])
async def test_review_can_quote_observed_json_text_without_reconstructing_its_structure(
    kb_dir, model_service, invented_closing_brace
):
    metadata = {"context": json.dumps({"coverage": {"status": "partial", "missing": 2}})}
    atomic_write_text(kb_dir / "wiki/sources/rows.md", json.dumps(metadata))
    model_service.chat_response = _reader("Coverage is partial.")
    model_service.chat_without_tools = True

    def review(body):
        payload = json.loads(body["messages"][-1]["content"])
        quote = '"coverage": {"status": "partial"'
        if invented_closing_brace:
            quote += "}"
        value = answer_review_response(payload)
        value["units"][0]["support"] = [{"observation": "o1", "quote": quote}]
        return {"role": "assistant", "content": json.dumps(value)}

    model_service.answer_review_response = review
    result = await continue_conversation(kb_dir, "What coverage status was observed?")
    assert (result.status == "completed") != invented_closing_brace, result


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["transient", "permanent", "foreign_unit"])
async def test_failed_batch_cannot_discard_previous_reviews_or_expand_edits(
    kb_dir, model_service, damage
):
    answer = "\n".join(f"Channel {i}: {7400 + i}" for i in range(1, 13))
    atomic_write_text(kb_dir / "wiki/sources/rows.md", answer)
    model_service.chat_response = _reader(answer)
    model_service.chat_without_tools = True
    reviews = []

    def review(body):
        payload = json.loads(body["messages"][-1]["content"])
        reviews.append(payload)
        value = answer_review_response(payload)
        if len(reviews) == 2 or (len(reviews) > 2 and damage != "transient"):
            if damage == "foreign_unit":
                value.update(
                    verdict="unsupported",
                    issues=[
                        {
                            "kind": "unsupported",
                            "claim": "Channel 1",
                            "units": ["u1"],
                            "reason": "An unassigned unit cannot authorize an edit.",
                        }
                    ],
                )
            else:
                value["units"] = []
        return {"role": "assistant", "content": json.dumps(value)}

    model_service.answer_review_response = review
    result = await continue_conversation(kb_dir, "List all channels.")
    assert (result.status == "completed") == (damage == "transient"), result
    assert len(reviews) == 3
    assert reviews[1] == {k: v for k, v in reviews[2].items() if k != "protocol_feedback"}
    assert reviews[2]["protocol_feedback"]["error"]
    assert not {u["id"] for u in reviews[0]["units"]} & {u["id"] for u in reviews[1]["units"]}
    assert result.usage["observable_attempts"] == 5


@pytest.mark.asyncio
async def test_identical_observations_share_storage_but_keep_read_order(kb_dir, model_service):
    answer = "Channel 4: standby only"
    atomic_write_text(kb_dir / "wiki/sources/rows.md", answer)
    reader = _reader(answer)
    reviews = []

    def chat(body):
        value = reader(body)
        if value.get("tool_calls"):
            call = value["tool_calls"][0]
            value["tool_calls"].append({**call, "id": "second-read"})
        return value

    def review(body):
        payload = json.loads(body["messages"][-1]["content"])
        reviews.append(payload)
        records = [o for o in payload["observations"] if o.get("name") == "read_file"]
        assert len(records) == 1 and records[0]["output"] == answer
        assert payload["observation_order"][:2] == [records[0]["id"]] * 2
        return {"role": "assistant", "content": json.dumps(answer_review_response(payload))}

    model_service.chat_response = chat
    model_service.chat_without_tools = True
    model_service.answer_review_response = review
    result = await continue_conversation(kb_dir, "What is channel 4's condition?")
    assert result.status == "completed" and result.answer == answer, result
    assert len(reviews) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("request_limit", [None, 3])
async def test_length_stopped_reviews_split_without_losing_units_or_scope(
    kb_dir, model_service, request_limit
):
    if request_limit:
        from openkb.application.settings import apply_kb_config_patch
        from openkb.application.settings_data import KbConfigPatchRequest
        from openkb.processing import DEFAULT_PROCESSING

        apply_kb_config_patch(
            kb_dir,
            KbConfigPatchRequest(
                kb=str(kb_dir),
                config={"processing": {**DEFAULT_PROCESSING, "max_requests": request_limit}},
            ),
        )
    answer = "\n".join(f"Channel {i}: {7400 + i}" for i in range(1, 13))
    atomic_write_text(kb_dir / "wiki/sources/rows.md", answer)
    model_service.chat_response = _reader(answer)
    model_service.chat_without_tools = True
    reviews = []

    def review(body):
        payload = json.loads(body["messages"][-1]["content"])
        reviews.append(payload)
        model_service.finish_reason = "length" if len(payload["units"]) > 4 else "stop"
        assert payload["answer"] == answer
        assert payload["observations"][0]["output"] == answer
        return {"role": "assistant", "content": json.dumps(answer_review_response(payload))}

    model_service.answer_review_response = review
    result = await continue_conversation(kb_dir, "List all channels and values.")
    if request_limit:
        assert result.status != "completed"
        assert len(model_service) == result.usage["observable_attempts"] == request_limit
        return
    assert result.status == "completed" and result.answer == answer, result
    successful = [p for p in reviews if len(p["units"]) <= 4]
    ids = [u["id"] for p in successful for u in p["units"]]
    assert len(ids) == len(set(ids)) == 12
    assert len(reviews) == 4 and result.usage["observable_attempts"] == 6


@pytest.mark.asyncio
async def test_protocol_feedback_repairs_only_the_review_binding(kb_dir, model_service):
    source = {"coverage": {"status": "partial"}}
    answer = "Coverage is partial."
    atomic_write_text(kb_dir / "wiki/sources/rows.md", json.dumps(source))
    model_service.chat_response = _reader(answer)
    model_service.chat_without_tools = True
    reviews = []

    def review(body):
        payload = json.loads(body["messages"][-1]["content"])
        reviews.append(payload)
        feedback = payload.get("protocol_feedback", {}).get("error")
        if feedback:
            assert feedback["problem"] == "support_mismatch"
            assert feedback["unit"] == "u1" and feedback["support"]["value"] == "complete"
        assert payload["answer"] == answer and payload["observations"][0]["output"] == source
        value = answer_review_response(payload)
        value["units"][0]["support"] = [
            {
                "observation": "o1",
                "path": ["coverage", "status"],
                "value": "partial" if feedback else "complete",
            }
        ]
        return {"role": "assistant", "content": json.dumps(value)}

    model_service.answer_review_response = review
    result = await continue_conversation(kb_dir, "Is source coverage complete?")
    assert result.status == "completed" and result.answer == answer, result
    assert len(reviews) == 2 and result.usage["observable_attempts"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage", ["wrapper", "real_wrapper", "nested", "string_index", "persistent"]
)
async def test_metadata_path_feedback_identifies_the_failed_step_without_guessing(
    kb_dir, model_service, damage
):
    source = {"figures": [{"bbox": [11.0, 22.0]}]}
    if damage == "real_wrapper":
        source["output"] = {"note": "a real source field"}
    answer = "The figure starts at 11."
    atomic_write_text(kb_dir / "wiki/sources/rows.md", json.dumps(source))
    model_service.chat_response = _reader(answer)
    model_service.chat_without_tools = True
    reviews = []
    correct = ["figures", 0, "bbox", 0]
    wrong = {
        "wrapper": ["output", *correct],
        "real_wrapper": ["output", *correct],
        "persistent": ["output", *correct],
        "nested": ["figures", 0, "missing"],
        "string_index": ["figures", "0", "bbox", 0],
    }[damage]

    def review(body):
        payload = json.loads(body["messages"][-1]["content"])
        reviews.append(payload)
        feedback = payload.get("protocol_feedback", {}).get("error")
        if feedback:
            assert feedback["problem"] == "support_mismatch"
            detail = feedback["binding_error"]
            assert detail["path_root"] == "observation.output"
            assert detail["reason"] == "invalid_path"
            if damage in {"wrapper", "persistent"}:
                assert detail["step"] == 0 and detail["container_type"] == "object"
                assert detail["available_keys"] == ["figures"]
            elif damage == "real_wrapper":
                assert detail["step"] == 1 and detail["container_type"] == "object"
                assert detail["available_keys"] == ["note"]
            elif damage == "nested":
                assert detail["step"] == 2 and detail["available_keys"] == ["bbox"]
            else:
                assert detail["step"] == 1 and detail["container_type"] == "array"
                assert detail["length"] == 1
        assert payload["answer"] == answer and payload["observations"][0]["output"] == source
        value = answer_review_response(payload)
        value["units"][0]["support"] = [
            {
                "observation": "o1",
                "path": correct if feedback and damage != "persistent" else wrong,
                "value": 11,
            }
        ]
        return {"role": "assistant", "content": json.dumps(value)}

    model_service.answer_review_response = review
    result = await continue_conversation(kb_dir, "Where does the figure start?")
    assert (result.status == "completed") == (damage != "persistent"), result
    assert len(reviews) == 2 and result.usage["observable_attempts"] == 4


@pytest.mark.asyncio
async def test_a_truncated_single_unit_never_authorizes_completion(kb_dir, model_service):
    atomic_write_text(kb_dir / "wiki/sources/rows.md", "Count: 10")
    model_service.chat_response = _reader("Count: 10")
    model_service.chat_without_tools = True

    def review(body):
        model_service.finish_reason = "length"
        return {"role": "assistant", "content": '{"verdict":"supported"}'}

    model_service.answer_review_response = review
    result = await continue_conversation(kb_dir, "What is the count?")
    assert result.status != "completed" and result.turn_count == 0
    assert result.usage["observable_attempts"] == 3
