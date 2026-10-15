"""Ambiguous untrusted JSON must never choose a page value or edit silently."""

import json

import pytest

from ultrakb.agent.page_response import PageResponseError, parse_page, parse_summary
from ultrakb.agent.page_updates import PageUpdate


@pytest.mark.unit
@pytest.mark.parametrize("strict", [False, True])
def test_duplicate_page_content_is_rejected_instead_of_choosing_the_last_value(strict):
    response = '{"description":"Operating rule","content":"Disconnect first","content":"Leave on"}'

    with pytest.raises(PageResponseError, match="Duplicate field: content"):
        parse_page(response, strict=strict)


@pytest.mark.unit
def test_duplicate_update_digest_cannot_replace_a_wrong_captured_base():
    original = "Disconnect the load before rewiring.\n"
    update = PageUpdate(original)
    response = (
        '{"status":"updated","base_sha256":"wrong-version","base_sha256":'
        + json.dumps(update.request()["base_sha256"])
        + ',"edits":[{"block_id":"b1","operation":"replace","content":"Use a lockout.\\n"}]}'
    )

    with pytest.raises(PageResponseError, match="page_update_invalid"):
        update.apply(response, current=original)


@pytest.mark.unit
@pytest.mark.parametrize(
    "response",
    [
        '{"description":"Rule","content":"First","content":"Second",}',
        '{description:"Rule",content:"First",content:"Second"}',
        "{'description':'Rule','content':'First','content':'Second'}",
        '{"description":"Rule","content":"First","evidence":{"id":"r1","id":"r2"},}',
    ],
)
def test_page_syntax_repair_must_not_erase_ambiguous_fields(response):
    with pytest.raises(PageResponseError, match="page_duplicate_field"):
        parse_page(response)


@pytest.mark.unit
@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("comment", ["/* note */", "// note\n", "# note\n"])
def test_page_repair_rejects_duplicate_keys_separated_from_colons_by_comments(quote, comment):
    response = (
        '{"description":"Rule","content":"First",'
        + quote
        + "content"
        + quote
        + " "
        + comment
        + ' :"Second",}'
    )

    with pytest.raises(PageResponseError, match="page_duplicate_field: Duplicate field: content"):
        parse_page(response)


@pytest.mark.unit
@pytest.mark.parametrize(
    "fields,duplicate",
    [
        ('"content":"First","con\\u0074ent":"Second"', "content"),
        ('"description":"First","description":"Second","content":"Rule"', "description"),
        ('"content":"Rule","source_ranges":["unknown"],"source_ranges":["r1"]', "source_ranges"),
        ('"content":"Rule","source_ranges":[{"id":"r1","id":"r2"}]', "id"),
    ],
)
def test_page_protocol_rejects_duplicate_members_at_every_depth(fields, duplicate):
    with pytest.raises(PageResponseError, match=f"Duplicate field: {duplicate}"):
        parse_page("{" + fields + "}")


@pytest.mark.unit
@pytest.mark.parametrize(
    "fields,duplicate",
    [
        ('"description":"First","description":"Second","sections":[]', "description"),
        ('"description":"Rule","sections":[{"id":"s1","id":"s2","content":"Rule"}]', "id"),
        (
            '"description":"Rule","sections":[{"id":"s1","content":"First","content":"Second"}]',
            "content",
        ),
    ],
)
def test_summary_protocol_rejects_ambiguous_fields_before_section_validation(fields, duplicate):
    with pytest.raises(
        PageResponseError, match=f"summary_invalid_json: Duplicate field: {duplicate}"
    ):
        parse_summary("{" + fields + "}", ("s1",))


@pytest.mark.unit
@pytest.mark.parametrize(
    "fields,duplicate",
    [
        ('"edits":[],"edits":[{"block_id":"b1","operation":"delete"}]', "edits"),
        (
            '"edits":[{"block_id":"b1","operation":"delete","operation":"replace",'
            '"content":"Use lockout.\\n"}]',
            "operation",
        ),
        (
            '"edits":[{"block_id":"b1","operation":"replace",'
            '"content":"Leave on.\\n","content":"Use lockout.\\n"}]',
            "content",
        ),
        (
            '"source_ranges":["unknown"],"source_ranges":["r1"],'
            '"edits":[{"block_id":"b1","operation":"delete"}]',
            "source_ranges",
        ),
    ],
)
def test_update_protocol_rejects_ambiguous_edit_and_evidence_members(fields, duplicate):
    original = "Disconnect the load before rewiring.\n"
    update = PageUpdate(original)
    response = '{"status":"updated","base_sha256":' + json.dumps(update.digest) + "," + fields + "}"

    with pytest.raises(PageResponseError, match="page_update_invalid") as caught:
        update.apply(response, current=original)
    assert str(caught.value.__cause__) == f"Duplicate field: {duplicate}"


@pytest.mark.unit
@pytest.mark.parametrize(
    "response,content",
    [
        (
            '{"description":"Rule","content":"Keep \\"content\\": in prose.",}',
            'Keep "content": in prose.',
        ),
        (
            "{'description':'Rule','content':'Keep \"content\": in prose.'}",
            'Keep "content": in prose.',
        ),
        (
            '{description:"Rule",content:"Keep content: in prose."}',
            "Keep content: in prose.",
        ),
        (
            '{"description":"Rule","content":"Use lockout.",'
            '"source_ranges":[{"id":"r1"},{"id":"r2"}],}',
            "Use lockout.",
        ),
        (
            '{"description":"Rule","content":"Use lockout.", /* "content":"comment" */}',
            "Use lockout.",
        ),
        (
            '{"description":"Rule","content":"Use lockout.", // "content":"comment"\n}',
            "Use lockout.",
        ),
        (
            '{"description":"Rule","content":"Use lockout.", # "content":"comment"\n}',
            "Use lockout.",
        ),
    ],
)
def test_unambiguous_page_syntax_repair_preserves_quoted_content_and_separate_objects(
    response, content
):
    result = parse_page(response)
    assert result["description"] == "Rule"
    assert result["content"] == content
