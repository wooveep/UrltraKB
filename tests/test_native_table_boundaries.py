"""Literal HTML examples and malformed tags cannot establish cell facts."""

import hashlib

import pytest
from test_native_table_projection import TABLE

from openkb.agent.evidence_payload import project_evidence
from openkb.source_qualification import MARKDOWN_POLICY
from openkb.text_measurement import measure_markdown
from openkb.text_source import FrozenText, TextOrigin, read_text_selection


def source(body):
    frozen = FrozenText(
        text=body,
        original_characters=len(body),
        original_digest=hashlib.sha256(body.encode()).hexdigest(),
        origins=(TextOrigin(normalized_span=(0, len(body)), original_span=(0, len(body))),),
        tokens=measure_markdown(body),
        normalization_policy=MARKDOWN_POLICY,
        assets={},
    )
    return read_text_selection(frozen.model_dump(mode="json"))


@pytest.mark.parametrize(
    "opening,closing",
    [
        ("<pre>", "</pre>"),
        ("<code>", "</code>"),
        ("<pre><code>", "</code></pre>"),
        ("<textarea>", "</textarea>"),
    ],
)
def test_html_literal_context_does_not_certify_table_cells(opening, closing):
    selected = source(opening + "\n" + TABLE + "\n" + closing)

    evidence = project_evidence(selected)

    assert evidence["content"] == selected["content"]
    assert all(t["status"] != "observed" for t in evidence.get("native_tables", []))


def test_literal_table_does_not_hide_following_real_table_in_same_html_block():
    body = "<div>\n<pre><code>" + TABLE + "</code></pre>\n" + TABLE + "\n</div>"
    selected = source(body)

    evidence = project_evidence(selected)

    assert evidence["content"] == selected["content"]
    (table,) = evidence["native_tables"]
    assert table["status"] == "observed"
    assert table["native_location"]["source_spans"][0][0] == body.rindex("<table>")


@pytest.mark.parametrize(
    "body",
    [
        "<table><tr><td/>A<td>B</td></tr></table>",
        "<table><tr><th/>A<td>B</td></tr></table>",
        "<table><tr/><tr><td>A</td></tr></table>",
        "<table><tbody/><tr><td>A</td></tr></table>",
    ],
)
def test_self_closing_table_elements_leave_structure_unresolved(body):
    selected = source(body)

    evidence = project_evidence(selected)

    assert evidence["content"] == selected["content"]
    (table,) = evidence["native_tables"]
    assert table["status"] == "unresolved" and table["diagnostic"]
    assert not {"rows", "cells", "value"}.intersection(table)


def test_literal_cell_text_and_void_line_break_preserve_real_cell_value():
    selected = source("<table><tr><td><code>Alpha</code><br/>Beta</td></tr></table>")

    evidence = project_evidence(selected)

    assert evidence["content"] == selected["content"]
    (table,) = evidence["native_tables"]
    assert table["status"] == "observed"
    (cell,) = table["cells"]
    assert cell["value"] == "Alpha\nBeta"


@pytest.mark.parametrize("opening", ["<code>", "Example <code>", "<xmp>"])
def test_literal_ancestors_survive_markdown_blocks_until_their_closing_tag(opening):
    closing = "</xmp>" if opening == "<xmp>" else "</code>"
    body = opening + "\n\n" + TABLE + "\n\n" + closing + "\n\n" + TABLE
    selected = source(body)

    evidence = project_evidence(selected)

    assert evidence["content"] == selected["content"]
    (table,) = evidence["native_tables"]
    assert table["status"] == "observed"
    assert table["native_location"]["source_spans"][0][0] == body.rindex("<table>")


@pytest.mark.parametrize(
    "tag", ["script", "style", "xmp", "iframe", "noembed", "noframes", "textarea", "title"]
)
def test_raw_html_text_does_not_certify_nested_examples_or_leak_after_close(tag):
    body = (
        f"<div><{tag}>\n\n<code>\n\n"
        + TABLE
        + f"\n\n</{tag}>\n"
        + TABLE
        + "\n</div>\n\nExample <code>\n\n"
        + TABLE
        + "\n\n</code>"
    )
    selected = source(body)

    evidence = project_evidence(selected)

    assert evidence["content"] == selected["content"]
    (table,) = evidence["native_tables"]
    assert table["status"] == "observed"
    assert table["native_location"]["source_spans"][0][0] == body.index(
        "<table>", body.index(f"</{tag}>")
    )


def test_plaintext_html_keeps_its_tail_literal_even_after_a_closing_tag():
    selected = source("<plaintext>\n\n</plaintext>\n\n" + TABLE)

    evidence = project_evidence(selected)

    assert evidence["content"] == selected["content"]
    assert all(t["status"] != "observed" for t in evidence.get("native_tables", []))
