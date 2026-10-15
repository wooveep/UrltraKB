"""Public compilation must not publish invented standalone path spellings."""

import json

import pytest

pytest_plugins = ("test_vendor_sdk",)


@pytest.mark.parametrize(
    "inline",
    [
        "https://example.test/a",
        "//example.test/a",
        "../concepts/a.md",
        "sources/a.md",
        "192.0.2.0/24",
        "/24",
        "/2/3",
        "6 / 2",
        "/usr/bin/check --verbose",
        r"C:\Tools\check.exe --verbose",
        r"C:\Tools\check.exe argument",
    ],
)
def test_non_path_inline_expressions_are_not_treated_as_path_transcriptions(inline):
    from ultrakb.agent.source_excerpts import SourceExcerpts

    content = f"Example: `{inline}`."
    assert SourceExcerpts(["Ordinary source prose."]).render(content) == content


@pytest.mark.parametrize(
    "source,path",
    [
        ("Read /system/stateful.", "/system/state"),
        ("Read /system/state.ini.", "/system/state"),
        ("Read /home/cafe\u0301/data.", "/home/cafe"),
        ("Read /prefix/system/state.", "/system/state"),
        ("Read /system/\nstate.", "/system/state"),
        ("```text\n/system/&lt;node&gt;/state\n```", "/system/<node>/state"),
        ("<!-- /system/&lt;node&gt;/state -->", "/system/<node>/state"),
        (
            '<table data-path="/system/&lt;node&gt;/state"><tr><td>Other</td></tr></table>',
            "/system/<node>/state",
        ),
        (
            "<table><tr><td><noscript>/system/&lt;node&gt;/state</noscript></td></tr></table>",
            "/system/<node>/state",
        ),
        (
            "<table><tr><td><title>/system/&lt;node&gt;/state</title></td></tr></table>",
            "/system/<node>/state",
        ),
        (
            "<table><tr><td>/system/&lt;node&gt;/<br/>state</td></tr></table>",
            "/system/<node>/state",
        ),
    ],
)
def test_other_spellings_and_extraction_breaks_do_not_certify_a_path(source, path):
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.agent.source_excerpts import SourceExcerpts

    with pytest.raises(PageResponseError, match="does not occur verbatim"):
        SourceExcerpts([source]).render(f"Read `{path}`.")


def test_missing_window_cannot_use_old_page_paths_as_original_evidence():
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.agent.source_excerpts import SourceExcerpts

    excerpts = SourceExcerpts([])
    response = json.dumps({"description": "Recovery", "content": "Read `/system/state`."})
    with pytest.raises(PageResponseError, match="does not occur verbatim"):
        excerpts.response(response, "page-json")


@pytest.mark.parametrize("ending", [".", ". Next sentence.", ", then continue.", "。"])
def test_sentence_punctuation_is_not_part_of_the_source_path(ending):
    from ultrakb.agent.source_excerpts import SourceExcerpts

    content = "During recovery inspect `/system/config/state`."
    excerpts = SourceExcerpts(["During recovery inspect /system/config/state" + ending])
    assert excerpts.render(content) == content


def test_complete_path_next_to_a_native_cell_break_keeps_exact_unicode_spelling():
    from test_native_table_projection import native_page

    from ultrakb.agent.evidence_payload import project_evidence
    from ultrakb.agent.source_excerpts import SourceExcerpts
    from ultrakb.source_pages import read_page_selection

    original = "<table><tr><td>Recovery:<br/>/home/cafe\u0301/&lt;node&gt;/state</td></tr></table>"
    content = "Inspect `/home/cafe\u0301/<node>/state`."
    evidence = project_evidence(read_page_selection([native_page(original)]))
    assert SourceExcerpts([evidence]).render(content) == content


@pytest.mark.parametrize(
    "code",
    [
        "/system/config/<节点>/state/",
        "<table><tr><td>/system/config/<节点>/state/</td></tr></table>",
    ],
)
def test_qualified_html_cell_path_does_not_authorize_a_new_code_excerpt(kb_dir, code):
    from test_source_qualification import freeze_source

    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.agent.source_excerpts import SourceExcerpts
    from ultrakb.agent.source_windows import CompileSourceWindows

    original = "<table><tr><td>/system/config/&lt;节点&gt;/state/</td></tr></table>"
    freeze_source(kb_dir, kb_dir, "md", original)
    window = next(
        CompileSourceWindows(kb_dir / "wiki", "manual").iter_document_windows(fits=lambda _: True)
    )
    excerpts = SourceExcerpts([window])
    inline = "Inspect `/system/config/<节点>/state/`."
    assert excerpts.render(inline) == inline
    with pytest.raises(PageResponseError, match="code excerpt changes or truncates"):
        excerpts.render(f"```text\n{code}\n```\n\n[[original:x1.1]]")
    with pytest.raises(PageResponseError, match="does not occur verbatim"):
        excerpts.render("Inspect `/systen/config/<节点>/state/`.")
    preserved = excerpts.render(f"{inline}\n\n[[original:x1.1]]")
    assert original in preserved and "[[original:" not in preserved
    assert (kb_dir / "wiki/sources/manual.md").read_text() == original


def _qualified_html_table(kb_dir):
    from test_source_qualification import freeze_source

    from ultrakb.agent.source_windows import CompileSourceWindows

    source = "<h1>Guide</h1><table><tr><td>Known</td></tr></table>"
    path, _ = freeze_source(kb_dir, kb_dir, "html", source)
    return path, CompileSourceWindows(kb_dir / "wiki", "manual").current_original_evidence()


def test_observed_html_table_locator_is_a_source_coordinate(kb_dir):
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.agent.source_excerpts import SourceExcerpts

    path, evidence = _qualified_html_table(kb_dir)
    before = path.read_bytes()
    excerpts = SourceExcerpts([evidence])
    content = "The supplied table is located at `/table[1]`."
    assert excerpts.render(content) == content
    for wrong in ("/table[2]", "/table[1]/state", "/system/config/state"):
        with pytest.raises(PageResponseError, match="does not occur verbatim"):
            excerpts.render(f"Read `{wrong}`.")
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "partial",
        "unresolved",
        "polluted",
        "wrong_span",
        "wrong_origin",
        "invalid_origin",
    ],
)
def test_unqualified_html_table_metadata_cannot_certify_a_path(kb_dir, damage):
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.agent.source_excerpts import SourceExcerpts

    path, evidence = _qualified_html_table(kb_dir)
    before = path.read_bytes()
    table = evidence["native_tables"][0]
    if damage == "missing":
        evidence.pop("native_tables")
    elif damage == "partial":
        table["coverage"] = "partial"
    elif damage == "unresolved":
        table["status"] = "unresolved"
    elif damage == "polluted":
        table["path"] = "/table[1]"
    elif damage == "wrong_span":
        table["native_location"]["source_spans"][0][0] += 1
    else:
        origin = next(o for o in evidence["origins"] if o.get("html", {}).get("tag") == "table")
        if damage == "wrong_origin":
            origin["html"]["path"] = "/system/config/state"
        else:
            origin["html"]["path"] = 123
    with pytest.raises(PageResponseError, match="does not occur verbatim"):
        SourceExcerpts([evidence]).render("The source coordinate is `/table[1]`.")
    assert path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["content", "description"])
@pytest.mark.parametrize(
    "original,path,wrong",
    [
        (
            "Inspect /system/config/cluster/<节点>/state/ on the recovery node.",
            "/system/config/cluster/<节点>/state/",
            "/systen/config/cluster/<节点>/state/",
        ),
        (
            "查看/sys/kernel/config/cluster/<你自己的目录>/node/ 是否存在其他异常主机",
            "/sys/kernel/config/cluster/<你自己的目录>/node/",
            "/syk/kernel/config/cluster/<你自己的目录>/node/",
        ),
        (
            r"Inspect C:\Program Files\Cluster\state.ini on the recovery node.",
            r"C:\Program Files\Cluster\state.ini",
            r"C:\Program Flies\Cluster\state.ini",
        ),
        (
            "<table><tr><td>/system/config/&lt;节点&gt;/state/</td></tr></table>",
            "/system/config/<节点>/state/",
            "/systen/config/<节点>/state/",
        ),
    ],
)
async def test_long_summary_repairs_changed_inline_path_before_publication(
    kb_dir, monkeypatch, live_llm, field, original, path, wrong
):
    from test_source_qualification import freeze_source

    from ultrakb.agent.compiler import _llm_call_page_async, _write_summary
    from ultrakb.agent.document_summary import SYSTEM_TEMPLATE, summary_messages, window_sections
    from ultrakb.agent.page_response import parse_summary
    from ultrakb.agent.source_excerpts import SourceExcerpts
    from ultrakb.agent.source_windows import CompileSourceWindows

    wiki = kb_dir / "wiki"
    freeze_source(kb_dir, kb_dir, "md", original)
    summary = wiki / "summaries/manual.md"
    reader = CompileSourceWindows(wiki, "manual")
    window = next(reader.iter_document_windows(fits=lambda _: True))
    sections = tuple(s["id"] for s in window_sections(window))
    invalid = json.dumps(
        {
            "description": f"Read `{wrong}`." if field == "description" else "Recovery",
            "sections": [
                {"id": section, "content": f"Read `{wrong}`." if field == "content" else original}
                for section in sections
            ],
        }
    )
    messages = summary_messages(
        {"role": "system", "content": SYSTEM_TEMPLATE.format(language="en")},
        "manual",
        window,
    )
    live_llm.install(monkeypatch)
    before = len(live_llm.calls)
    raw = await _llm_call_page_async(
        "deepseek/deepseek-flash",
        messages,
        "document-summary:1",
        bundle=live_llm.credentials,
        initial_response=invalid,
        numeric_sources=[original],
        original_excerpts=SourceExcerpts([window]),
        response_protocol="summary-json",
        expected_sections=sections,
        response_format={"type": "json_object"},
        max_tokens=512,
    )
    decoded = parse_summary(raw, sections)
    assert len(live_llm.calls) - before == 1
    assert "page_literal_conflict" in str(live_llm.calls[-1]["messages"])
    _write_summary(
        wiki,
        "manual",
        "\n\n".join(s["content"] for s in decoded["sections"]),
        description=decoded["description"],
        full_text="sources/manual.md",
    )
    import html

    visible = html.unescape(summary.read_text())
    assert path in visible and wrong not in visible
    assert (wiki / "sources/manual.md").read_text() == original


@pytest.mark.parametrize(
    "original, proposed",
    [
        ("Inspect /system/config/state on the recovery node.", "/systen/config/state"),
        ("读取/目录/system/config/state。", "/system/config/state"),
        ("Read prefix/system/config/state.", "/system/config/state"),
        ("```text\n目录/system/config/state\n```", "/system/config/state"),
        ("Read `目录/system/config/state`.", "/system/config/state"),
        ("Read cafe\u0301/system/config/state.", "/system/config/state"),
        ("[manual](目录/system/config/state)", "/system/config/state"),
        ("![manual](目录/system/config/state)", "/system/config/state"),
        ("<code>\n\n目录/system/config/state\n\n</code>", "/system/config/state"),
        ("<xmp>\n\n目录/system/config/state\n\n</xmp>", "/system/config/state"),
        ("<title>\n\n目录/system/config/state\n\n</title>", "/system/config/state"),
    ],
)
def test_repeated_path_corruption_keeps_the_previous_summary(kb_dir, original, proposed):
    from types import SimpleNamespace

    from ultrakb.agent.page_delivery import validate_page_delivery
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.agent.source_excerpts import SourceExcerpts
    from ultrakb.compile_checkpoints import CompileCheckpoints
    from ultrakb.locks import atomic_write_text

    summary = kb_dir / "wiki/summaries/manual.md"
    atomic_write_text(summary, "Previous successful knowledge.\n")
    before = summary.read_bytes()
    checkpoint = CompileCheckpoints(
        kb_dir,
        SimpleNamespace(task_id="a" * 32, parent_task_id=None),
        {"document": "manual"},
        {"protocol": "summary-json"},
    )
    invalid = json.dumps(
        {
            "description": "Recovery",
            "sections": [
                {"id": "section-1", "content": f"Read `{proposed}`."},
            ],
        }
    )
    for _ in range(2):
        with pytest.raises(PageResponseError, match="page_literal_conflict"):
            validate_page_delivery(
                invalid,
                response_protocol="summary-json",
                expected_sections=("section-1",),
                original_excerpts=SourceExcerpts([original]),
            )
    checkpoint.claim_repair("summary:1", "same-request")
    checkpoint.record_failure("summary:1", "same-request", "page_literal_conflict")
    restarted = CompileCheckpoints(
        kb_dir,
        SimpleNamespace(task_id="a" * 32, parent_task_id=None),
        {"document": "manual"},
        {"protocol": "summary-json"},
    )
    with pytest.raises(PageResponseError, match="compile_repair_exhausted"):
        restarted.require_generation("summary:1", "same-request")
    assert restarted.load("summary:1", "same-request") is None
    assert summary.read_bytes() == before


@pytest.mark.parametrize("echo_description", [False, True])
def test_update_repairs_only_new_path_literals_through_the_sdk(kb_dir, echo_description):
    from publication_inputs import update_inputs

    from ultrakb.agent.page_delivery import validate_page_delivery
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.agent.source_excerpts import SourceExcerpts

    source = "During recovery inspect /system/config/state.\n"
    description = "Read `/legacy/unknown`."
    old = (
        f"---\ndescription: {description}\n---\n\n"
        "Preserved legacy path `/legacy/unknown`.\n\nOld recovery rule.\n"
    )
    page, reader, originals, patch, _, _ = update_inputs(kb_dir, old, source)
    excerpts = SourceExcerpts(originals.windows)
    invalid = json.dumps(
        {
            "base_sha256": patch.digest,
            **({"description": description} if echo_description else {}),
            "edits": [
                {
                    "block_id": "b2",
                    "operation": "replace",
                    "content": "During recovery inspect `/systen/config/state`.",
                }
            ],
        }
    )
    with pytest.raises(PageResponseError, match="page_literal_conflict"):
        validate_page_delivery(
            invalid, page_update=patch, read_current=page.read_text, original_excerpts=excerpts
        )
    valid = invalid.replace("/systen/config/state", "/system/config/state")
    result = json.loads(
        validate_page_delivery(
            valid,
            page_update=patch,
            read_current=page.read_text,
            original_excerpts=excerpts,
        )
    )
    assert "`/system/config/state`" in result["content"]
    assert "`/legacy/unknown`" in result["content"]
    assert result["description"] == description
    assert page.read_text() == old
