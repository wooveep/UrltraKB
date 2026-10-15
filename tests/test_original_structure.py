"""Only source syntax evidence can turn a text boundary into a heading."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from test_answer_evidence_regression import agent_for, invoke

from ultrakb.content_blocks import BLOCK_POLICY, split_blocks
from ultrakb.text_formats import text_normalization_policy
from ultrakb.text_source import NORMALIZATION_POLICY, FrozenText, TextOrigin


def frozen_text(kb_dir, text, *, policy=NORMALIZATION_POLICY, blocks=False):
    raw = FrozenText(
        text=text,
        original_characters=len(text),
        original_digest=hashlib.sha256(text.encode()).hexdigest(),
        origins=(TextOrigin(normalized_span=(0, len(text)), original_span=(0, len(text))),),
        assets={},
        tokens=0,
        normalization_policy=policy or NORMALIZATION_POLICY,
        unit_kind="block" if blocks else "text",
        blocks=split_blocks(text) if blocks else (),
        block_policy=BLOCK_POLICY if blocks else None,
    ).model_dump(mode="json")
    if policy is None:
        raw.pop("normalization_policy")
    (kb_dir / "wiki/sources/manual.content.json").write_text(json.dumps(raw))
    return raw


def native_text(kb_dir, text, kind):
    if kind in {"page", "slide"}:
        bodies = [text, "7.4 Later section\ncapacity after a physical boundary."]
        (kb_dir / "wiki/sources/manual.json").write_text(
            json.dumps(
                [
                    {
                        "page": n,
                        "unit_kind": "page",
                        "content": body,
                        **({"parts": {"body": body, "notes": ""}} if kind == "slide" else {}),
                    }
                    for n, body in enumerate(bodies, 1)
                ]
            )
        )
    elif kind == "legacy":
        (kb_dir / "wiki/sources/manual.md").write_text(text)
    else:
        frozen_text(kb_dir, text, policy=text_normalization_policy("txt"), blocks=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["page", "slide", "txt_blocks", "legacy"])
@pytest.mark.parametrize(
    "marker",
    [
        "# cobbler distro list",
        "# Ordinary prose",
        "4.4.3.4 Section name",
        "192.166.201.110 command argument",
        "192.168.999.1 invalid address or identifier",
        "9.4.0 Release version",
        "2.5 Measured quantity",
        "2026.10.13 Recorded date",
        "1. Execute a step",
    ],
)
async def test_unstructured_markers_are_not_certified_headings(kb_dir, kind, marker):
    body = "7.3 Earlier section\n" + marker + "\ncapacity is preserved verbatim."
    native_text(kb_dir, body, kind)
    agent = agent_for(kb_dir)
    result = json.loads(await invoke(agent, "search_originals", terms=["capacity"]))
    assert result["hits"]
    readbacks = []
    for hit in result["hits"]:
        scope = hit["source_context"]["regions"][0]
        assert scope["status"] == "unconfirmed" and scope["headings"] == []
        action = hit["reading_action"]
        read = await invoke(agent, action["tool"], **action["arguments"])
        assert "capacity" in read
        readbacks.append(read)
    assert any(marker in read for read in readbacks)


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [None, "unknown-format-v1", text_normalization_policy("html")])
async def test_missing_or_unproved_format_cannot_certify_markdown(kb_dir, policy):
    frozen_text(kb_dir, "# Apparent heading\ncapacity remains.", policy=policy, blocks=True)
    result = json.loads(await invoke(agent_for(kb_dir), "search_originals", terms=["capacity"]))
    scope = result["hits"][0]["source_context"]["regions"][0]
    assert scope["status"] == "unconfirmed" and not scope["headings"]


@pytest.mark.asyncio
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
async def test_explicit_markdown_preserves_real_structure_not_numeric_prose(kb_dir, newline):
    text = newline.join(
        [
            "Root title",
            "==========",
            "",
            "## Actual section",
            "",
            "192.166.201.110 command argument",
            "9.4.0 Release",
            "1. Ordered step",
            "",
            "capacity belongs to the actual section.",
        ]
    )
    frozen_text(kb_dir, text)
    result = json.loads(await invoke(agent_for(kb_dir), "search_originals", terms=["capacity"]))
    scope = result["hits"][0]["source_context"]["regions"][0]
    assert scope["status"] == "source_heading"
    assert [h["text"] for h in scope["headings"]] == ["Root title", "Actual section"]
    for heading in scope["headings"]:
        assert text[heading["offset"] :].startswith(heading["literal"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "nested", ["> # Quoted heading\n> quoted text", "- # List heading\n  list text"]
)
async def test_nested_markdown_heading_does_not_escape_its_container(kb_dir, nested):
    frozen_text(kb_dir, "# Real root\n\n" + nested + "\n\ncapacity outside the container.")
    result = json.loads(await invoke(agent_for(kb_dir), "search_originals", terms=["capacity"]))
    scope = result["hits"][0]["source_context"]["regions"][0]
    assert [h["literal"] for h in scope["headings"]] == ["# Real root"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["code", "xmp", "title", "table", "div", "textarea", "script"])
async def test_html_container_cannot_supply_a_markdown_heading(kb_dir, tag):
    text = f"# Real root\n\n<{tag}>\n\n# False example\n\n</{tag}>\n\ncapacity outside."
    frozen_text(kb_dir, text)
    result = json.loads(await invoke(agent_for(kb_dir), "search_originals", terms=["capacity"]))
    scope = result["hits"][0]["source_context"]["regions"][0]
    assert [h["literal"] for h in scope["headings"]] == ["# Real root"]


@pytest.mark.asyncio
async def test_html_heading_requires_its_saved_native_tag(kb_dir, tmp_path):
    from ultrakb.html_source import freeze_html

    path = tmp_path / "manual.html"
    path.write_text(
        "<h1>Real root</h1><p># ordinary text</p><h2>Actual section</h2>"
        "<p>9.4.0 Release<br>capacity belongs to this section.</p>"
    )
    prepared = SimpleNamespace(
        path=path, source=path, images={}, digest=hashlib.sha256(path.read_bytes()).hexdigest()
    )
    frozen = freeze_html(prepared, "manual", kb_dir / "wiki")
    (kb_dir / "wiki/sources/manual.content.json").write_text(frozen.model_dump_json())
    result = json.loads(await invoke(agent_for(kb_dir), "search_originals", terms=["capacity"]))
    scope = result["hits"][0]["source_context"]["regions"][0]
    assert scope["status"] == "source_heading"
    assert [h["text"] for h in scope["headings"]] == ["Real root", "Actual section"]


@pytest.mark.asyncio
async def test_same_text_different_syntax_does_not_share_heading_authority(kb_dir):
    text = "# Declared only in Markdown\ncapacity remains."
    for policy, expected in [
        (NORMALIZATION_POLICY, "source_heading"),
        (text_normalization_policy("txt"), "unconfirmed"),
        (NORMALIZATION_POLICY, "source_heading"),
    ]:
        frozen_text(kb_dir, text, policy=policy)
        result = json.loads(await invoke(agent_for(kb_dir), "search_originals", terms=["capacity"]))
        assert result["hits"][0]["source_context"]["regions"][0]["status"] == expected
