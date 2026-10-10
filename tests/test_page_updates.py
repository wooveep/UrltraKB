"""Temporary page patches preserve everything outside the explicitly edited blocks."""

import json

import pytest


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_patch_keeps_unedited_tables_fences_and_whitespace(newline):
    from openkb.agent.page_updates import PageUpdate

    body = (
        "\n# Valve\n\nCapacity: 12 litres.\n\n"
        "| Condition | Mode |\n| --- | --- |\n| Winter | Closed |\n\n"
        "```text\n## Not a heading\nvalue = 12\n```\n\nOther source: 72 hours.  \n"
    )
    body = body.replace("\n", newline)
    original = "---\ntype: Concept\n---\n".replace("\n", newline) + body
    update = PageUpdate(original)
    request = update.request()
    target = next(block["id"] for block in request["blocks"] if "Capacity:" in block["text"])
    response = json.dumps(
        {
            "base_sha256": request["base_sha256"],
            "description": "Capacity",
            "edits": [
                {
                    "block_id": target,
                    "operation": "replace",
                    "content": "Capacity: 36 litres." + newline,
                }
            ],
        }
    )
    result = update.apply(response, current=original)
    assert result["content"] == body.replace("Capacity: 12 litres.", "Capacity: 36 litres.")
    assert all(
        "Not a heading" not in block["text"] or block["text"].startswith("```")
        for block in request["blocks"]
    )


@pytest.mark.parametrize("bad", ["base", "block", "conflict", "operation", "stale"])
def test_patch_rejects_uncertain_targets_and_changed_pages(bad):
    from openkb.agent.page_response import PageResponseError
    from openkb.agent.page_updates import PageUpdate

    original = "# Conditions\n\nOnly during winter.\n"
    update = PageUpdate(original)
    data = update.request()
    edit = {"block_id": data["blocks"][-1]["id"], "operation": "replace", "content": "New fact.\n"}
    response = {"base_sha256": data["base_sha256"], "edits": [edit]}
    if bad == "base":
        response["base_sha256"] = "different-version"
    elif bad == "block":
        edit["block_id"] = "missing"
    elif bad == "operation":
        edit["operation"] = {}
    elif bad == "conflict":
        response["edits"].append({**edit, "operation": "delete"})
    with pytest.raises(PageResponseError):
        update.apply(json.dumps(response), current="Human edit." if bad == "stale" else original)


def test_untouched_duplicate_numeric_bindings_survive_an_unrelated_insert():
    from openkb.agent.page_updates import PageUpdate

    table = (
        "| Object | Condition | Latency |\n| --- | --- | --- |\n"
        "| Valve | Winter | 24 ms |\n| Valve | Winter | 36 ms |\n"
    )
    original = table + "\nRetained clarification.\n"
    update = PageUpdate(
        original, originals=[table.replace("24 ms", "12 ms").replace("36 ms", "12 ms")]
    )
    data = update.request()
    result = update.apply(
        json.dumps(
            {
                "base_sha256": data["base_sha256"],
                "edits": [
                    {
                        "block_id": data["blocks"][-1]["id"],
                        "operation": "insert_after",
                        "content": "\nNew source clarification.\n",
                    }
                ],
            }
        ),
        current=original,
    )
    assert result["content"] == original + "\nNew source clarification.\n"


def test_link_filtering_only_changes_new_edits():
    from openkb.agent.page_updates import PageUpdate

    original = "# Context\n\nRetained [[concepts/old-target]] relationship.\n"
    update = PageUpdate(original, known_targets={"concepts/allowed"})
    data = update.request()
    result = update.apply(
        json.dumps(
            {
                "base_sha256": data["base_sha256"],
                "edits": [
                    {
                        "block_id": data["blocks"][-1]["id"],
                        "operation": "insert_after",
                        "content": "\n\nNew [[concepts/invalid]] and [[concepts/allowed]].\n",
                    }
                ],
            }
        ),
        current=original,
    )
    assert "Retained [[concepts/old-target]] relationship." in result["content"]
    assert "New invalid and [[concepts/allowed]]." in result["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [False, True])
async def test_compiler_patch_repair_is_bounded_at_transport(monkeypatch, stale):
    from types import SimpleNamespace

    from openkb.agent.compiler import _llm_call_page_async
    from openkb.agent.page_response import PageResponseError
    from openkb.agent.page_updates import PageUpdate

    original = "# Conditions\n\nWinter rule.\n"
    update = PageUpdate(original)
    data = update.request()
    sends = []

    async def response(**kwargs):
        sends.append(kwargs)
        reply = {
            "base_sha256": "bad" if len(sends) == 1 else data["base_sha256"],
            "edits": [
                {
                    "block_id": data["blocks"][-1]["id"],
                    "operation": "insert_after",
                    "content": "\nNew supported condition.\n",
                }
            ],
        }
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(reply)), finish_reason="stop"
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    monkeypatch.setattr("litellm.acompletion", response)
    if stale:
        with pytest.raises(PageResponseError, match="page_update_stale"):
            await _llm_call_page_async(
                "test-model",
                [update.message()],
                "update",
                page_update=update,
                read_current=lambda: "Human change.",
            )
        assert len(sends) == 1
    else:
        result = json.loads(
            await _llm_call_page_async(
                "test-model",
                [update.message()],
                "update",
                page_update=update,
                read_current=lambda: original,
            )
        )
        assert "Winter rule." in result["content"]
        assert "New supported condition." in result["content"]
        assert len(sends) == 2


@pytest.mark.parametrize(
    "value,status",
    [("0.012 s", "checked_numeric_subset"), ("24 ms", "conflict"), ("12 kg", "conflict")],
)
def test_numeric_table_check_binds_object_attribute_unit_and_condition(value, status):
    from openkb.agent.page_updates import check_numeric_subset

    table = (
        "# Limits\n\n| Object | Attribute | Condition | Value |\n"
        "| --- | --- | --- | --- |\n| River Valve | Latency | Winter | {} |\n"
    )
    result = check_numeric_subset(table.format(value), [table.format("12 ms")])
    assert result["status"] == status


@pytest.mark.parametrize(
    "equation,status",
    [
        ("7 * 5 = 35", "checked_numeric_subset"),
        ("7 * 5 = 34", "conflict"),
        ("1 / 3 = 0.33", "not_applicable"),
        ("__import__('os').system('false') = 0", "not_applicable"),
    ],
)
def test_numeric_check_allows_derived_values_and_limits_arithmetic(equation, status):
    from openkb.agent.page_updates import check_numeric_subset

    assert check_numeric_subset(equation, ["Inputs are seven and five."])["status"] == status


def test_numeric_check_leaves_ambiguous_bindings_and_unchanged_old_content_alone():
    from openkb.agent.page_updates import check_numeric_subset

    template = "Object: 42\nCondition: Winter\nLatency: {}\n"
    assert (
        check_numeric_subset(
            template.format("24 ms"), [template.format("12 ms"), template.format("36 ms")]
        )["status"]
        == "not_applicable"
    )
    assert (
        check_numeric_subset(
            template.format("24 ms"), [template.format("12 ms")], previous=template.format("24 ms")
        )["status"]
        == "not_applicable"
    )
    assert (
        check_numeric_subset(
            "The recommended policy may improve capacity.", [template.format("12 ms")]
        )["status"]
        == "not_applicable"
    )
