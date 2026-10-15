"""Inspect native document controls and empty/loading/error distinctions without model sends."""

import argparse
import json
import os
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "true"
    os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
    if args.headless:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from publication_fixtures import fail_document
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QAbstractButton, QPushButton

    from ultrakb import config
    from ultrakb.application import document_actions
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.desktop.documents import DocumentsDialog
    from ultrakb.desktop.source_reader import SourceReader
    from ultrakb.desktop.verification import create_application
    from ultrakb.desktop.verification_workbench import button
    from ultrakb.desktop.window import Workbench
    from ultrakb.locks import atomic_write_text, kb_ingest_lock
    from ultrakb.state import HashRegistry

    config.GLOBAL_CONFIG_DIR = root / "settings"
    config.GLOBAL_CONFIG_PATH = config.GLOBAL_CONFIG_DIR / "global.yaml"
    kb = root / "资料体验库"
    initialize_kb(kb, seed_environment=False)
    app = create_application()
    app.setQuitOnLastWindowClosed(False)
    assert args.headless or app.platformName() not in {"offscreen", "minimal"}
    window = Workbench(history_dir=root / "tasks")
    window.resize(1366, 768)
    window.show()
    checks = []

    def wait(condition, timeout=35):
        deadline = time.monotonic() + timeout
        while not condition():
            app.processEvents()
            time.sleep(0.01)
            if time.monotonic() > deadline:
                raise TimeoutError("Documents acceptance timed out")
        app.processEvents()

    try:
        window.open_knowledge_base(kb)
        wait(lambda: window.kb == kb and not window.io._callbacks)
        button(window, "资料").click()
        wait(lambda: "资料" in window.workspaces.panels)
        panel = window.workspaces.panels["资料"][0]
        assert isinstance(panel, DocumentsDialog)
        wait(lambda: "暂无资料" in panel.status.text())
        assert not window.remote_assets.isVisible()
        assert not panel.keep_raw.isVisible() and not panel.confirm_button.isVisible()
        assert not any(
            b.isVisible() and b.text() == "重编译全部资料"
            for b in panel.findChildren(QAbstractButton)
        )
        importer = button(window, "导入资料")
        assert [a.text() for a in importer.menu().actions()] == ["导入文件", "导入目录", "导入网址"]
        panel.grab().save(str(root / "empty.png"))
        checks.append("A07/A17: single import menu, folded options, truthful first empty state")
        with kb_ingest_lock(kb / ".ultrakb"):
            atomic_write_text(
                kb / "wiki/sources/独立内部名称.md", "# 可读原文\n\n阅读不会发送模型请求。"
            )
            registry = HashRegistry(kb / ".ultrakb/hashes.json")
            registry.add(
                "a" * 64, {"name": "可读资料.md", "doc_name": "独立内部名称", "type": "md"}
            )
            registry.add("b" * 64, {"name": "正文缺失.pdf", "doc_name": "missing", "type": "pdf"})
            atomic_write_text(kb / "raw/未完成资料.md", "# 已保存正文\n\n本地中断，尚未形成知识。")
        fail_document(
            kb, kb / "raw/未完成资料.md", bundle=LlmCredentialBundle(api_key="unused-fixture")
        )
        panel.reload()
        wait(lambda: panel.table.rowCount() == 3)
        failed = next(row for row in panel.rows.values() if row.document["name"] == "未完成资料.md")
        assert failed.actions["read"].allowed and failed.actions["retry"].allowed
        assert not failed.actions["ask"].allowed and not failed.actions["recompile"].allowed
        missing = next(row for row in panel.rows.values() if row.document["name"] == "正文缺失.pdf")
        assert not missing.actions["read"].allowed
        panel.table.selectRow(
            next(i for i in range(3) if panel.table.item(i, 0).text() == "正文缺失.pdf")
        )
        assert "正文" in panel.status.text()
        panel.grab().save(str(root / "qualified-rows.png"))
        checks.append(
            "Real storage fixtures: unfinished retained text is readable; "
            "missing body has visible reason"
        )
        panel.search.setFocus()
        QTest.keyClicks(panel.search, "does-not-exist")
        assert panel.table.rowCount() == 0 and "筛选" in panel.status.text()
        panel.search.clear()
        assert panel.table.rowCount() == 3
        from PySide6.QtWidgets import QToolButton

        more = next(
            b
            for b in panel.findChildren(QToolButton)
            if b.accessibleName() == "更多 正文缺失.pdf" and b.isVisible()
        )
        QTest.mouseClick(more, Qt.MouseButton.LeftButton)
        assert any(
            not action.isEnabled() and "正文" in action.text()
            for action in panel._row_menu.actions()
        )
        panel._row_menu.close()
        panel.state_filter.setCurrentText("未完成")
        assert panel.table.rowCount() == 1
        panel.state_filter.setCurrentText("全部")
        assert all(panel.table.cellWidget(i, j) is None for i in range(3) for j in range(3))
        checks.append(
            "A17: search/filter empty differs from no documents; state filter uses eligibility"
        )
        read = next(
            b
            for b in panel.findChildren(QPushButton)
            if b.accessibleName() == "阅读 可读资料.md" and b.isVisible()
        )
        QTest.mouseClick(read, Qt.MouseButton.LeftButton)
        wait(lambda: any(d.isVisible() for d in window.findChildren(SourceReader)))
        reader = next(d for d in window.findChildren(SourceReader) if d.isVisible())
        assert "阅读不会" in reader.reader.toPlainText()
        reader.close()
        panel.reload()
        assert panel.table.rowCount() == 3 and "刷新" in panel.status.text()
        wait(lambda: not window.io._callbacks)
        original = document_actions.document_inventory

        def failing(*_args, **_kwargs):
            raise OSError("fixture local read failure")

        document_actions.document_inventory = failing
        try:
            panel.reload()
            wait(lambda: "读取资料失败" in panel.status.text())
            assert panel.table.rowCount() == 3
            panel.grab().save(str(root / "error-preserves-rows.png"))
        finally:
            document_actions.document_inventory = original
        checks.append("A17: refresh and local read failure retain current rows; row reading works")
        button(window, "知识").click()
        knowledge = button(window, "知识维护")
        assert len(knowledge.menu().actions()) == 3
        button(window, "任务").click()
        assert button(window, "待处理工作").menu().actions()
        checks.append(
            "A07: knowledge regeneration/refresh/proposals and pending tasks remain reachable"
        )
        button(window, "资料").click()
        panel.reload()
        wait(lambda: not window.io._callbacks)
        for theme in ("浅色", "深色"):
            window.theme.setCurrentText(theme)
            for width, height in ((1366, 768), (720, 600)):
                window.resize(width, height)
                app.processEvents()
                assert panel.search.isVisible() and panel.table.isVisible()
                window.grab().save(str(root / f"{theme}-{width}.png"))
        assert not window.manager.tasks()
        report = {
            "platform": app.platformName(),
            "headless": args.headless,
            "checks": checks,
            "model_sends": 0,
            "fixtures": "real retained state/local budget interruption; no model response",
        }
    finally:
        window.request_quit()
        wait(window.shutdown_complete)
    (root / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
