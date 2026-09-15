"""Generation clicks expose original omissions; stale asynchronous reads cannot replace them."""

from openkb.application.documents import import_document
from openkb.desktop.source_review import SourceReview
from tests.test_compilation_omissions import setup as omission_fixture
from tests.test_source_flow_ui import app as app_fixture
from tests.test_source_flow_ui import window as window_fixture

app, window, setup = app_fixture, window_fixture, omission_fixture


def test_generation_opens_excluded_original_and_other_steps_have_specific_views(
    window, kb_dir, setup
):
    source, _, calls = setup
    result = import_document(kb_dir, source)
    before = calls.copy()
    panel = SourceReview(window, kb_dir, result.source_id, stage="generation")
    panel.show()
    view = panel.issue_view
    assert panel.tabs.currentIndex() == panel.issue_tab
    assert "Beta requirement." in view.text.toPlainText()
    assert "Alpha requirement." not in view.text.toPlainText()
    assert "未通过原文证据复核" in view.reason.text()
    assert "已排除 1 项" in view.status.text()
    assert "concepts/beta" in view.items.currentText()
    panel.show_stage("facts")
    assert panel.tabs.tabText(5) == "事实与引文" and panel.record_evidence.isVisible()
    panel.show_stage("planning")
    assert panel.tabs.tabText(5) == "主题与对应事实"
    assert "Beta requirement." in panel.record_evidence.toPlainText()
    panel.show_stage("generation")
    assert panel.tabs.tabText(5) == "生成稿与校验"
    assert not panel.record_evidence.isVisible()
    assert "Beta requirement." in view.text.toPlainText()
    assert not window.io.errors and not window.manager.requests and calls == before
    view.original.click()
    assert panel._selected_stage == "parsing"
    assert "Beta requirement." in panel.content.toPlainText()
    assert not window.io.errors


def test_leaving_generation_discards_delayed_original_read(window, kb_dir, setup):
    source, _, _ = setup
    result = import_document(kb_dir, source)
    panel = SourceReview(window, kb_dir, result.source_id, stage="generation")
    queued = []
    window.io.submit = lambda operation, callback, **kw: queued.append((operation, callback))
    panel.issue_view.select()
    assert len(queued) == 1
    panel.show_stage("planning")
    panel.issue_view.text.setPlainText("new selection")
    operation, callback = queued[0]
    callback(operation(), None)
    assert panel.issue_view.text.toPlainText() == "new selection"


def test_original_can_be_read_to_the_end_in_bounded_chunks(window, kb_dir, setup, monkeypatch):
    from openkb.application.source_actions import read_source_evidence

    source, _, _ = setup
    result = import_document(kb_dir, source)
    monkeypatch.setattr(
        "openkb.desktop.source_issue_view.read_source_evidence",
        lambda kb, ref, **kw: read_source_evidence(kb, ref, max_chars=4),
    )
    panel = SourceReview(window, kb_dir, result.source_id, stage="generation")
    view = panel.issue_view
    assert "Beta" in view.text.toPlainText() and view.more.isEnabled()
    chunks = [view.text.toPlainText().split("原文内容：\n", 1)[1]]
    for _ in range(10):
        if not view.more.isEnabled():
            break
        view.more.click()
        chunks.append(view.text.toPlainText().split("原文内容：\n", 1)[1])
    assert "".join(chunks) == "Beta requirement."
    assert not view.more.isEnabled() and not window.io.errors
