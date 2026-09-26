"""Suggestions become executable only after bounded original-source reads."""

import pytest

from openkb.agent.document_plan import PagePlan
from openkb.evidence import BlockDraft, ParseStore
from tests.test_source_evidence import save_source


def _source(kb_dir, tmp_path):
    path = tmp_path / "instructions.txt"
    path.write_text("Original manual")
    source = save_source(kb_dir, path)
    store = ParseStore(kb_dir)
    parsed = store.save(
        source,
        {"parser": "resolution-test"},
        [
            BlockDraft("# Credentials", "heading", {"kind": "text", "headings": ["Credentials"]}),
            BlockDraft(
                "Obtain the access token first.",
                "paragraph",
                {"kind": "text", "headings": ["Credentials"]},
            ),
            BlockDraft("# Install", "heading", {"kind": "text", "headings": ["Install"]}),
            BlockDraft(
                "Run setup after obtaining credentials.",
                "paragraph",
                {"kind": "text", "headings": ["Install"]},
            ),
            BlockDraft("# Appendix", "heading", {"kind": "text", "headings": ["Appendix"]}),
            BlockDraft(
                "Unrelated appendix.", "paragraph", {"kind": "text", "headings": ["Appendix"]}
            ),
        ],
    )
    return source, parsed, store.reader(source, parsed)


def _page(hints):
    return PagePlan(
        key="install",
        kind="concept",
        name="concepts/install",
        title="Installation",
        purpose="Describe installation",
        subject_ranges=[],
        state="pending_evidence",
        scope_resolution=None,
        location_hints=hints,
    )


def test_unindexed_original_heading_and_prerequisite_are_read_before_ready(kb_dir, tmp_path):
    from openkb.agent.document_page_resolution import prepare_page

    source, parsed, reader = _source(kb_dir, tmp_path)
    page = _page(
        [{"role": "subject", "value": "Install"}, {"role": "context", "value": "Credentials"}]
    )
    result = prepare_page(page, source, parsed, None, reader)
    assert result.page.state == "ready"
    assert result.page.subject_ranges == [[2, 4]]
    assert result.page.context_ranges == [[0, 2]]
    assert {row["text"] for row in result.evidence["blocks"]} == {
        "# Credentials",
        "Obtain the access token first.",
        "# Install",
        "Run setup after obtaining credentials.",
    }


def test_unknown_hint_skips_one_page_but_io_failure_is_not_content_success(kb_dir, tmp_path):
    from openkb.agent.document_page_resolution import prepare_page

    source, parsed, reader = _source(kb_dir, tmp_path)
    missing = prepare_page(
        _page([{"role": "subject", "value": "section:absent"}]), source, parsed, None, reader
    )
    assert missing.page.state == "skipped" and missing.reason
    assert not missing.page.subject_ranges
    page = _page([{"role": "subject", "value": "Install"}])

    class BrokenReader:
        def read(self, *args, **kwargs):
            raise OSError("storage failure")

        def complete_bound(self, reference):
            return reader.complete_bound(reference)

    with pytest.raises(OSError, match="storage failure"):
        prepare_page(page, source, parsed, None, BrokenReader())


@pytest.mark.parametrize("separator", ["、", ",", "，", ";", "\n"])
@pytest.mark.parametrize("role", ["subject", "related"])
def test_multiple_keys_remain_distinct_required_or_optional_selections(
    kb_dir, tmp_path, separator, role
):
    from openkb.agent.document_page_resolution import prepare_page

    source, parsed, reader = _source(kb_dir, tmp_path)
    navigation = {
        "nodes": [
            {"id": "credentials", "parent": None, "start": 0, "end": 2, "title": "Credentials"},
            {"id": "install", "parent": None, "start": 2, "end": 4, "title": "Install"},
        ]
    }
    value = separator.join(["section:credentials", "section:install"])
    result = prepare_page(
        _page([{"role": role, "value": value}]), source, parsed, navigation, reader
    )
    assert result.page.state == "ready"
    assert {row["text"] for row in result.evidence["blocks"]} == {
        "# Credentials",
        "Obtain the access token first.",
        "# Install",
        "Run setup after obtaining credentials.",
    }
    missing = prepare_page(
        _page([{"role": role, "value": value + separator + "section:missing"}]),
        source,
        parsed,
        navigation,
        reader,
    )
    assert missing.page.state == ("skipped" if role == "subject" else "ready")
    assert any("missing" in note or "unresolved" in note for note in missing.page.planning_notes)


@pytest.mark.parametrize("separator", [" > ", " → ", " › ", " / "])
def test_heading_paths_remove_display_wrappers_per_segment(kb_dir, tmp_path, separator):
    from openkb.agent.document_page_resolution import prepare_page

    source, parsed, reader = _source(kb_dir, tmp_path)
    navigation = {
        "nodes": [
            {"id": "manual", "parent": None, "start": 0, "end": 6, "title": "Manual"},
            {"id": "install", "parent": "manual", "start": 2, "end": 4, "title": "Install"},
        ]
    }
    page = _page([{"role": "subject", "value": separator.join(["`Manual`", "「Install」"])}])
    result = prepare_page(page, source, parsed, navigation, reader)
    assert result.page.state == "ready" and result.page.subject_ranges == [[2, 4]]


def test_related_clues_are_alternatives_and_known_keys_survive_display_prose(kb_dir, tmp_path):
    from openkb.agent.document_page_resolution import prepare_page

    source, parsed, reader = _source(kb_dir, tmp_path)
    navigation = {
        "nodes": [{"id": "install", "parent": None, "start": 2, "end": 4, "title": "Install"}]
    }
    page = _page(
        [
            {"role": "related", "value": "Unconfirmed context; section:install (display hint)"},
            {"role": "related", "value": "Unknown optional relationship"},
        ]
    )
    result = prepare_page(page, source, parsed, navigation, reader)
    assert result.page.state == "ready"
    assert result.page.subject_ranges == [[2, 4]]
    assert any(
        row["text"] == "Run setup after obtaining credentials." for row in result.evidence["blocks"]
    )
    assert any("Unknown optional relationship" in note for note in result.page.planning_notes)


@pytest.mark.parametrize("other", ["Appendix", "Missing chapter"])
def test_subject_selection_does_not_drop_a_second_unindexed_heading(kb_dir, tmp_path, other):
    from openkb.agent.document_page_resolution import prepare_page

    source, parsed, reader = _source(kb_dir, tmp_path)
    navigation = {
        "nodes": [{"id": "install", "parent": None, "start": 2, "end": 4, "title": "Install"}]
    }
    result = prepare_page(
        _page([{"role": "subject", "value": f"section:install; {other}"}]),
        source,
        parsed,
        navigation,
        reader,
    )
    if other == "Appendix":
        assert result.page.state == "ready" and result.page.subject_ranges == [[2, 4], [4, 6]]
    else:
        assert result.page.state == "skipped" and not result.page.subject_ranges


def test_failed_multi_hint_preparation_is_atomic_across_continue(kb_dir, tmp_path):
    from openkb.agent.document_page_resolution import prepare_page

    source, parsed, reader = _source(kb_dir, tmp_path)
    page = _page([{"role": "subject", "value": ["Install", "section:absent"]}])
    first = prepare_page(page, source, parsed, None, reader)
    second = prepare_page(first.page, source, parsed, None, reader, retry_skipped=True)
    assert first.page.state == second.page.state == "skipped"
    assert first.page.subject_ranges == second.page.subject_ranges == []


def test_saved_ranges_do_not_hide_new_unindexed_subject_hints(kb_dir, tmp_path):
    from openkb.agent.document_page_resolution import prepare_page

    source, parsed, reader = _source(kb_dir, tmp_path)
    page = _page([{"role": "subject", "value": "Install"}])
    page.subject_ranges, page.scope_resolution = [[0, 2]], "section"
    result = prepare_page(page, source, parsed, None, reader)
    assert result.page.state == "ready"
    assert result.page.subject_ranges == [[0, 2], [2, 4]]
    assert any(
        row["text"] == "Run setup after obtaining credentials." for row in result.evidence["blocks"]
    )


@pytest.mark.parametrize("clue", [{"heading_path": ["Alpha", "Setup"]}, "Alpha > Setup", "Setup"])
def test_original_paths_and_page_description_disambiguate_leaf_titles(kb_dir, tmp_path, clue):
    from openkb.agent.document_page_resolution import prepare_page

    source, _, _ = _source(kb_dir, tmp_path)
    store = ParseStore(kb_dir)
    parsed = store.save(
        source,
        {"parser": "branches"},
        [
            BlockDraft("# Setup", "heading", {"kind": "text", "headings": [branch, "Setup"]})
            for branch in ("Alpha", "Beta")
        ],
    )
    page = _page([{"role": "subject", "value": clue}])
    page.title, page.purpose = "Alpha Setup", "Prepare Alpha"
    result = prepare_page(page, source, parsed, None, store.reader(source, parsed))
    assert result.page.state == "ready" and result.page.subject_ranges == [[0, 1]]
