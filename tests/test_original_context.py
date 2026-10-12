"""Original reader boundaries preserve scope and individual image deliveries."""

import json

import pytest
from PIL import Image
from test_answer_evidence_regression import agent_for, invoke


def pages(kb_dir, contents, images=()):
    (kb_dir / "wiki/sources/manual.json").write_text(
        json.dumps(
            [
                {
                    "page": n,
                    "unit_kind": "page",
                    "content": body,
                    "images": [{"path": p} for p in images],
                }
                for n, body in enumerate(contents, 1)
            ]
        )
    )


@pytest.mark.asyncio
async def test_same_field_keeps_distinct_original_operations(kb_dir):
    pages(
        kb_dir,
        ["# Create orchard\ncapacity is optional.\n# Extend irrigation\ncapacity is required."],
    )
    agent = agent_for(kb_dir)
    result = json.loads(await invoke(agent, "search_originals", terms=["capacity"]))
    assert len(result["hits"]) == 2
    scopes = [hit["source_context"]["regions"][0] for hit in result["hits"]]
    assert scopes[0]["headings"][-1]["text"] == "Create orchard"
    assert scopes[1]["headings"][-1]["text"] == "Extend irrigation"
    assert all(scope["status"] == "source_heading" for scope in scopes)
    assert all(hit["source_context"]["content_digest"] for hit in result["hits"])


@pytest.mark.asyncio
async def test_unknown_parent_is_explicit_and_adjacent_action_executes(kb_dir):
    pages(kb_dir, ["Unlabelled fragment", "Neighbor content"])
    agent = agent_for(kb_dir)
    result = await invoke(agent, "get_page_content", doc_name="manual", pages="1")
    context = json.loads(result.split("\nSource context: ")[1].split("\n")[0])
    assert context["regions"][0]["status"] == "unconfirmed"
    action = context["adjacent_actions"][-1]
    assert "Neighbor content" in await invoke(agent, action["tool"], **action["arguments"])


@pytest.mark.asyncio
@pytest.mark.parametrize("earlier", ["6. Configure an earlier device", "2 172 billion bytes"])
async def test_numbered_scope_does_not_inherit_an_unrelated_prefix(kb_dir, earlier):
    pages(
        kb_dir,
        [earlier, "4.4 Management\n4.4.1 Availability\nUse three nodes."],
    )
    agent = agent_for(kb_dir)
    result = await invoke(agent, "get_page_content", doc_name="manual", pages="2")
    context = json.loads(result.split("\nSource context: ")[1].split("\n")[0])
    assert "Use three nodes." in result
    scope = context["regions"][-1]
    assert [h["literal"] for h in scope["headings"]] == [
        "4.4 Management",
        "4.4.1 Availability",
    ]
    assert context["reading_action"]["arguments"]["pages"] == "2"


@pytest.mark.asyncio
async def test_numbered_scope_keeps_only_matching_ancestors_across_pages(kb_dir):
    pages(kb_dir, ["3 Features\n3.2 Storage", "3.6 Operations\n3.6.16 Clock\nUse five servers."])
    result = await invoke(agent_for(kb_dir), "get_page_content", doc_name="manual", pages="2")
    context = json.loads(result.split("\nSource context: ")[1].split("\n")[0])
    assert [h["literal"] for h in context["regions"][-1]["headings"]] == [
        "3 Features",
        "3.6 Operations",
        "3.6.16 Clock",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("earlier", "target", "expected"),
    [
        ("3 Earlier chapter\n## Context", "4.4.1 Different chapter", ["4.4.1 Different chapter"]),
        (
            "3.2 Earlier section\n### Context",
            "4.4.1.1 Different section",
            ["4.4.1.1 Different section"],
        ),
        (
            "3 Earlier chapter\n## Context",
            "3.4.1 Matching section",
            ["3 Earlier chapter", "## Context", "3.4.1 Matching section"],
        ),
    ],
)
async def test_markdown_heading_cannot_hide_a_mismatched_numbered_ancestor(
    kb_dir, earlier, target, expected
):
    pages(kb_dir, [earlier, target + "\nNative target."])
    result = await invoke(agent_for(kb_dir), "get_page_content", doc_name="manual", pages="2")
    context = json.loads(result.split("\nSource context: ")[1].split("\n")[0])
    assert [h["literal"] for h in context["regions"][-1]["headings"]] == expected
    assert "Native target." in result
    assert context["reading_action"]["arguments"]["pages"] == "2"


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["5.2Deployment modes", "5.2部署模式"])
async def test_uncertain_numbered_boundaries_separate_native_operations(kb_dir, title):
    first = "capacity requires two managers.\n"
    next_title = "5.3Deployment steps"
    body = first + next_title + "\ncapacity is restricted to testing."
    pages(kb_dir, ["5.1.3 Earlier topology", title, body])
    agent = agent_for(kb_dir)
    result = json.loads(await invoke(agent, "search_originals", terms=["capacity"]))
    assert len(result["hits"]) == 2
    scopes = [hit["source_context"]["regions"][0] for hit in result["hits"]]
    assert all(scope["status"] == "unconfirmed" and not scope["headings"] for scope in scopes)
    assert scopes[0]["unconfirmed_boundary"] == {"unit": 2, "offset": 0, "literal": title}
    assert scopes[1]["unconfirmed_boundary"] == {
        "unit": 3,
        "offset": len(first),
        "literal": next_title,
    }
    assert "testing" not in scopes[0]["nearby_original"]
    assert "two managers" not in scopes[1]["nearby_original"]
    for hit in result["hits"]:
        assert hit["reading_action"]["arguments"]["pages"] == "3"
    read = await invoke(agent, "get_page_content", doc_name="manual", pages="3")
    context = json.loads(read.split("\nSource context: ")[1].split("\n")[0])
    assert context["regions"][0]["char_range"] == [0, len(first)]
    assert context["regions"][1]["char_range"] == [len(first), len(body)]
    assert context["regions"][0]["unconfirmed_boundary"] == scopes[0]["unconfirmed_boundary"]
    assert context["content_digest"] == result["hits"][0]["source_context"]["content_digest"]
    assert body in json.loads(read.split("\n\n", 1)[1].split("\nSource context: ")[0])["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", ["2.5GHz", "192.168.1.2", "9.4.0Patch004", "9.4.0", "1.ssh command"]
)
async def test_unseparated_numbers_are_not_certified_as_headings(kb_dir, value):
    pages(kb_dir, ["# Confirmed scope\n" + value + "\ncapacity is recorded."])
    result = json.loads(await invoke(agent_for(kb_dir), "search_originals", terms=["capacity"]))
    scope = result["hits"][0]["source_context"]["regions"][0]
    assert all(h["literal"] == "# Confirmed scope" for h in scope["headings"])
    if "unconfirmed_boundary" in scope:
        assert scope["status"] == "unconfirmed"
        assert scope["unconfirmed_boundary"]["literal"] == value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "```text\n9.2 Example configuration\n5.2Literal value\n```",
        "~~~text\n9.2 Example configuration\n5.2Literal value\n~~~",
        "    9.2 Example configuration\n    5.2Literal value",
        "<pre>\n9.2 Example configuration\n5.2Literal value\n</pre>",
        "<code>\n9.2 Example configuration\n5.2Literal value\n</code>",
        "<code>\n\n9.2 Example configuration\n\n5.2Literal value\n\n</code>",
        "<xmp>\n\n9.2 Example configuration\n\n5.2Literal value\n\n</xmp>",
        "<title>\n\n9.2 Example configuration\n\n5.2Literal value\n\n</title>",
    ],
)
async def test_literal_blocks_do_not_change_original_scope(kb_dir, code):
    pages(kb_dir, ["# Confirmed operation\n\n" + code, "capacity is recorded."])
    agent = agent_for(kb_dir)
    result = json.loads(await invoke(agent, "search_originals", terms=["capacity"]))
    scope = result["hits"][0]["source_context"]["regions"][0]
    assert [h["literal"] for h in scope["headings"]] == ["# Confirmed operation"]
    assert "unconfirmed_boundary" not in scope
    read = await invoke(agent, "get_page_content", doc_name="manual", pages="1")
    assert "9.2 Example configuration" in read and "5.2Literal value" in read


@pytest.mark.asyncio
async def test_literal_context_continues_across_pages_then_confirmed_scope_restarts(kb_dir):
    pages(
        kb_dir,
        [
            "# Confirmed operation\n\n````text\n9.2 Example configuration",
            "5.2Literal value\n```\n9.3 More literal text\n````\ncapacity remains in operation.",
            "5.2Uncertain section\ncapacity has unknown scope.\n"
            "5.3 Known section\ncapacity is known.",
        ],
    )
    result = json.loads(await invoke(agent_for(kb_dir), "search_originals", terms=["capacity"]))
    scopes = [hit["source_context"]["regions"][0] for hit in result["hits"]]
    assert [h["literal"] for h in scopes[0]["headings"]] == ["# Confirmed operation"]
    assert scopes[1]["status"] == "unconfirmed" and not scopes[1]["headings"]
    assert [h["literal"] for h in scopes[2]["headings"]] == ["5.3 Known section"]


@pytest.mark.asyncio
@pytest.mark.parametrize("container", ["table", "div"])
@pytest.mark.parametrize("title", ["4.4.3.4.2 Clock sync", "4.4.3.4.2Clock sync"])
async def test_html_container_end_preserves_following_native_boundaries(kb_dir, container, title):
    pages(
        kb_dir,
        [
            "4.4.3 Management\n4.4.3.2 Network\n"
            f"<{container}>\n9.2 Example cell\n5.2Literal cell\n</{container}>\n"
            "4.4.3.4 Node preparation",
            title + "\ncapacity must match.\n4.4.3.4.3 Disks\ncapacity needs a disk.",
        ],
    )
    agent = agent_for(kb_dir)
    result = json.loads(await invoke(agent, "search_originals", terms=["capacity"]))
    assert len(result["hits"]) == 2
    first, second = [hit["source_context"]["regions"][0] for hit in result["hits"]]
    if "2 Clock" in title:
        assert [h["literal"] for h in first["headings"]] == [
            "4.4.3 Management",
            "4.4.3.4 Node preparation",
            title,
        ]
    else:
        assert first["status"] == "unconfirmed" and not first["headings"]
        assert first["unconfirmed_boundary"] == {"unit": 2, "offset": 0, "literal": title}
    assert "needs a disk" not in first["nearby_original"]
    assert second["headings"][-1]["literal"] == "4.4.3.4.3 Disks"
    read = await invoke(agent, "get_page_content", doc_name="manual", pages="1")
    context = json.loads(read.split("\nSource context: ")[1].split("\n")[0])
    assert all(
        h["literal"] not in {"9.2 Example cell", "5.2Literal cell"}
        for region in context["regions"]
        for h in region["headings"]
    )
    assert "9.2 Example cell" in read and "5.2Literal cell" in read


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "markup",
    [
        '<img alt="\n9.2 Example attribute\n5.2Literal attribute\n" src="figure.png">',
        "<!--\n9.2 Example comment\n5.2Literal comment\n-->",
        "<table>\n<tr><td>\n9.2 Example cell\n5.2Literal cell\n</td></tr>\n</table>",
    ],
)
async def test_html_syntax_does_not_certify_headings_or_hide_following_scope(kb_dir, markup):
    pages(kb_dir, ["# Real scope\n" + markup + "\ncapacity remains.\n3.2 Next\ncapacity changes."])
    result = json.loads(await invoke(agent_for(kb_dir), "search_originals", terms=["capacity"]))
    assert len(result["hits"]) == 2
    scopes = [hit["source_context"]["regions"][0] for hit in result["hits"]]
    assert [h["literal"] for h in scopes[0]["headings"]] == ["# Real scope"]
    assert scopes[1]["headings"][-1]["literal"] == "3.2 Next"


@pytest.mark.asyncio
@pytest.mark.parametrize("parameter", ["<vm>", "<snap-name>", "<hostname>"])
async def test_native_command_parameters_do_not_open_html_containers(kb_dir, parameter):
    pages(
        kb_dir,
        [
            "4.4.3.4.5 Snapshots\nvirsh snapshot-create-as " + parameter,
            "5.2Deployment modes\ncapacity belongs to this mode.\n"
            "5.3Deployment steps\ncapacity belongs to these steps.",
        ],
    )
    result = json.loads(await invoke(agent_for(kb_dir), "search_originals", terms=["capacity"]))
    assert len(result["hits"]) == 2
    scopes = [hit["source_context"]["regions"][0] for hit in result["hits"]]
    assert all(s["status"] == "unconfirmed" and not s["headings"] for s in scopes)
    assert [s["unconfirmed_boundary"]["literal"] for s in scopes] == [
        "5.2Deployment modes",
        "5.3Deployment steps",
    ]
    assert "these steps" not in scopes[0]["nearby_original"]


@pytest.mark.asyncio
async def test_each_retained_occurrence_has_its_own_location_without_pixel_actions(kb_dir):
    Image.new("RGB", (32, 32), "white").save(kb_dir / "wiki/sources/figure.png")
    pages(kb_dir, ["# First\nNative text", "# Second\nNative text"], ["sources/figure.png"])
    agent = agent_for(kb_dir)
    first = await invoke(agent, "get_page_content", doc_name="manual", pages="1")
    second = await invoke(agent, "get_page_content", doc_name="manual", pages="2")
    a = json.loads(first.split("\nImage coverage: ")[1])["occurrences"][0]
    b = json.loads(second.split("\nImage coverage: ")[1])["occurrences"][0]
    assert a["occurrence_id"] != b["occurrence_id"]
    assert a["path"] == b["path"]
    assert a["page"] == 1 and b["page"] == 2
    assert a["status"] == b["status"] == "unread"
    assert "reading_action" not in a and "reading_action" not in b
    assert "get_image" not in {tool.name for tool in agent.tools}


def test_projected_native_spans_define_context_coverage():
    from openkb.original_context import original_context

    source = {
        "path": "sources/sheet.content.json",
        "source_revision_id": "revision",
        "content_digest": "digest",
        "unit_kind": "worksheet",
        "readers": {},
        "reading_action": {"tool": "read_file", "arguments": {"path": "index.md"}},
    }
    raw = {"text": "A1 unrelated\nA2 target\nA3 unrelated"}
    context = original_context(source, raw, {"cells": "A2"}, selected={"source_spans": [[13, 22]]})
    assert context["regions"][0]["char_range"] == [13, 22]
    assert "A1" not in context["regions"][0]["nearby_original"]
    assert original_context(source, raw, {"chars": "0:36"}, selected={})["regions"] == []
