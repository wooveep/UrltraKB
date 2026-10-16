"""Source-backed repair is deterministic and never authorizes invented syntax."""

import json

import pytest

from ultrakb.agent.page_response import PageResponseError
from ultrakb.agent.source_excerpts import SourceExcerpts


def native(content):
    return {
        "content": content,
        "path": "sources/manual.json",
        "selection_offset": 0,
        "page_spans": [{"page": 70, "start": 0, "end": len(content)}],
    }


def fenced(body):
    return f"```text\n{body}\n```"


def test_structured_parts_copy_original_and_check_template_basis():
    source = native(
        "First example:\nhttps://example.test/api/vms/11111111-1111-4111-8111-111111111111/2?now=true\n\n"
        "Second example:\nhttps://example.test/api/vms/22222222-2222-4222-8222-222222222222/2?now=false\n"
    )
    excerpts = SourceExcerpts([source])
    response = {
        "description": "Two source examples",
        "sections": [
            {
                "id": "s1",
                "parts": [
                    {"kind": "prose", "text": "The source gives two examples.", "refs": []},
                    {"kind": "excerpt", "text": "", "refs": ["x1.1"]},
                    {
                        "kind": "derived_template",
                        "text": "/api/vms/{vmId}/2",
                        "refs": ["x1.1", "x1.2"],
                    },
                ],
            }
        ],
    }
    delivered = json.loads(excerpts.response(json.dumps(response), "summary-parts-json", ("s1",)))
    content = delivered["sections"][0]["content"]
    assert "https://example.test/api/vms/11111111" in content
    assert "/api/vms/{vmId}/2" in content
    assert "推导" in content
    assert "[[original:" not in content
    # Accepted checkpoint bodies remain self-verifiable after assembly.
    assert json.loads(excerpts.response(json.dumps(delivered), "summary-json", ("s1",)))


def test_structured_reference_cannot_select_unknown_source():
    value = {
        "description": "Guide",
        "sections": [
            {
                "id": "s1",
                "parts": [
                    {"kind": "excerpt", "text": "", "refs": ["unknown"]},
                ],
            }
        ],
    }
    with pytest.raises(PageResponseError) as caught:
        SourceExcerpts(["known"]).response(json.dumps(value), "summary-parts-json", ("s1",))
    assert caught.value.field_errors[0]["path"].startswith("$.sections[0].parts[0]")


def test_proved_original_reference_syntax_is_literal_data_after_copy_and_restart():
    excerpts = SourceExcerpts(["Keep the literal marker:\n[[original:x99.99]]\n"])
    first = excerpts.render("[[original:x1.1]]")
    assert "[[original:x99.99]]" in first
    assert excerpts.render(first).count("[[original:x99.99]]") == 1


def test_reference_furniture_keeps_printed_page_and_copyright_as_source_metadata():
    source = native("版权所有 Example\n第58页\n示例:\nsetting=KEEP\n")
    excerpts = SourceExcerpts([source])
    refs = excerpts.references
    [identity] = [key for key in refs if key.startswith("r")]
    locator = excerpts.index.locator(refs[identity])
    assert locator["physical_pages"] == [70]
    assert locator["printed_pages"][0]["text"] == "第58页"
    assert locator["copyright"][0]["text"] == "版权所有 Example"
    assert locator["context_spans"]
    assert "版权所有" not in excerpts.index.body(refs[identity])


def test_structured_references_can_select_the_second_identical_occurrence():
    first = native("X86 only.\n\nsetting=KEEP\n\n")
    second = {
        **native("ARM only.\n\nsetting=KEEP\n\n"),
        "selection_offset": 200,
        "page_spans": [{"page": 71, "start": 0, "end": 25}],
        "source_revision_id": "retained-revision",
    }
    excerpts = SourceExcerpts([first, second])
    identity = next(
        identity
        for identity, match in excerpts.references.items()
        if identity.startswith("r")
        and match.source == 2
        and excerpts.index.body(match) == "setting=KEEP"
    )
    response = json.dumps(
        {
            "description": "Processor-specific setting",
            "sections": [
                {
                    "id": "s1",
                    "parts": [
                        {"kind": "prose", "text": "ARM only.", "refs": []},
                        {"kind": "excerpt", "text": "", "refs": [identity]},
                    ],
                }
            ],
        }
    )
    # Reconstruct the reader as after a restart; identical text is not identical evidence.
    for reader in (excerpts, SourceExcerpts([first, second])):
        delivered = json.loads(reader.response(response, "summary-parts-json", ("s1",)))
        assert "ARM only." in delivered["sections"][0]["content"]
        assert "setting=KEEP" in delivered["sections"][0]["content"]
        [resolution] = reader.resolutions
        assert resolution["path"] == "$.sections[0].parts[1].refs"
        assert resolution["source"]["source_number"] == 2
        assert resolution["source"]["physical_pages"] == [71]
        assert resolution["source"]["window_span"] == [11, 24]
        assert resolution["source"]["selection_offset"] == 200
        assert resolution["source"]["source_revision_id"] == "retained-revision"


def test_structured_repair_uses_reference_fields_without_changing_legacy_hints():
    from ultrakb.agent.page_response import page_repair_message

    bad = json.dumps(
        {
            "description": "Guide",
            "sections": [{"id": "s1", "parts": [{"kind": "excerpt", "text": "invented"}]}],
        }
    )
    with pytest.raises(PageResponseError) as caught:
        SourceExcerpts(["original"]).response(bad, "summary-parts-json", ("s1",))
    message = page_repair_message(
        caught.value.code,
        str(caught.value),
        response_protocol="summary-parts-json",
        expected_sections=("s1",),
        field_errors=caught.value.field_errors,
    )["content"]
    assert "kind, text and refs" in message and "invented" not in message
    assert "use only id and content" not in message
