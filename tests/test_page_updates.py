"""Temporary page patches preserve everything outside the explicitly edited blocks."""

import json

import pytest


def test_explicit_unchanged_result_keeps_the_captured_page():
    from ultrakb.agent.page_updates import PageUpdate, UnchangedPage

    original = "---\ntype: Concept\n---\n# Conditions\n\nOnly during winter.\n"
    update = PageUpdate(original)
    result = update.apply(
        json.dumps(
            {
                "status": "unchanged",
                "base_sha256": update.request()["base_sha256"],
                "reason": "The supplied winter condition is already fully expressed.",
            }
        ),
        current=original,
    )
    assert isinstance(result, UnchangedPage)
    assert result.reason == "The supplied winter condition is already fully expressed."
    assert result.base_sha256 == update.request()["base_sha256"]


def test_metadata_only_update_preserves_body_and_validates_entity_type():
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.agent.page_updates import PageUpdate

    original = "---\ntype: product\ndescription: Wrong object\n---\n# Device\n\nRetained body.\n"
    update = PageUpdate(original, allowed_types=frozenset({"product", "other"}))
    proposal = {"base_sha256": update.digest, "description": "Device conditions", "edits": []}
    result = update.apply(json.dumps(proposal), current=original)
    assert result["content"] == "# Device\n\nRetained body.\n"
    assert result["description"] == "Device conditions"
    with pytest.raises(PageResponseError, match="type"):
        update.apply(json.dumps({**proposal, "type": "invented"}), current=original)
    with pytest.raises(PageResponseError):
        update.apply(json.dumps({**proposal, "description": "Wrong object"}), current=original)


@pytest.mark.parametrize("returned_type", ["Work", "work", "Other"])
def test_updates_use_the_same_type_vocabulary_as_published_frontmatter(returned_type):
    from ultrakb.agent.page_updates import PageUpdate

    original = "---\ntype: Work\ndescription: Manual\n---\n# Manual\n\nOld rule.\n"
    patch = PageUpdate(original, allowed_types=frozenset({"work", "other"}))
    response = {
        "base_sha256": patch.digest,
        "type": returned_type,
        "edits": [{"block_id": "b2", "operation": "replace", "content": "New rule.\n"}],
    }
    result = patch.apply(json.dumps(response), current=original)
    assert result["content"] == "# Manual\n\nNew rule.\n"
    assert result.get("type", patch.request()["type"]).lower() == returned_type.lower()
    if returned_type.lower() == "work":
        assert "type" not in result
    assert patch.request()["allowed_types"] == ["other", "work"]


@pytest.mark.parametrize("bad", ["extra", "empty", "shape", "hash", "stale"])
def test_unchanged_requires_a_valid_frozen_page_and_only_its_declared_fields(bad):
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.agent.page_updates import PageUpdate

    original = "# Conditions\n\nOnly during winter.\n"
    update = PageUpdate(original)
    result = {"status": "unchanged", "base_sha256": update.digest, "reason": "Already covered."}
    if bad == "extra":
        result["edits"] = []
    elif bad == "empty":
        result["reason"] = "  "
    elif bad == "shape":
        result["reason"] = ["Already covered."]
    elif bad == "hash":
        result["base_sha256"] = "wrong"
    with pytest.raises(PageResponseError):
        update.apply(json.dumps(result), current="Human edit" if bad == "stale" else original)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_patch_keeps_unedited_tables_fences_and_whitespace(newline):
    from ultrakb.agent.page_updates import PageUpdate

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
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.agent.page_updates import PageUpdate

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
    from ultrakb.agent.page_updates import PageUpdate

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
    from ultrakb.agent.page_updates import PageUpdate

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


@pytest.mark.parametrize("stale", [False, True])
def test_compiler_patch_repair_is_bounded_at_transport(kb_dir, stale):
    from test_compile_windows import _checkpoints

    from ultrakb.agent.page_delivery import validate_page_delivery
    from ultrakb.agent.page_response import PageResponseError, page_repair_message
    from ultrakb.agent.page_updates import PageUpdate

    original = "# Conditions\n\nWinter rule.\n"
    update = PageUpdate(original)
    data = update.request()
    edit = {
        "block_id": data["blocks"][-1]["id"],
        "operation": "insert_after",
        "content": "\nNew supported condition.\n",
    }
    checkpoints = _checkpoints(kb_dir)
    initial = json.dumps({"base_sha256": "bad", "edits": [edit]})
    expected = "page_update_stale" if stale else "page_update_invalid"
    with pytest.raises(PageResponseError, match=expected) as failure:
        validate_page_delivery(
            initial, page_update=update, read_current=lambda: "Human change." if stale else original
        )
    if stale:
        assert not checkpoints.path.exists()
    else:
        correction = page_repair_message(failure.value.code, str(failure.value), page_update=True)
        assert "captured base_sha256" in correction["content"]
        checkpoints.claim_repair("page", "same-request")
        result = json.loads(
            validate_page_delivery(
                json.dumps({"base_sha256": data["base_sha256"], "edits": [edit]}),
                page_update=update,
                read_current=lambda: original,
            )
        )
        assert "Winter rule." in result["content"]
        assert "New supported condition." in result["content"]
        with pytest.raises(PageResponseError, match="compile_repair_exhausted"):
            checkpoints.claim_repair("page", "same-request")


@pytest.mark.parametrize(
    "value,status",
    [("0.012 s", "checked_numeric_subset"), ("24 ms", "conflict"), ("12 kg", "conflict")],
)
def test_numeric_table_check_binds_object_attribute_unit_and_condition(value, status):
    from ultrakb.agent.page_updates import check_numeric_subset

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
    from ultrakb.agent.page_updates import check_numeric_subset

    assert check_numeric_subset(equation, ["Inputs are seven and five."])["status"] == status


def test_numeric_check_leaves_ambiguous_bindings_and_unchanged_old_content_alone():
    from ultrakb.agent.page_updates import check_numeric_subset

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
