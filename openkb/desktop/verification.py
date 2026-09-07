"""Opt-in real-window acceptance runner for source and frozen builds.

All generated data and settings live in a new, explicitly supplied directory.
This checks a subset of desktop behavior; it is not full product acceptance.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import threading
import time
from pathlib import Path


def main() -> int:
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-base", help="Controlled HTTP model fixture URL")
    parser.add_argument("--inputs", type=Path, help="Document fixtures to import through workers")
    parser.add_argument("--corpus", type=Path, help="Render corpus through the actual Qt reader")
    parser.add_argument("--url", help="Controlled HTTP article fixture")
    parser.add_argument("--one-shot-url", help="Controlled PDF URL that can be downloaded once")
    args = parser.parse_args()
    root = args.output.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False)

    from PySide6.QtGui import QFontDatabase
    from PySide6.QtWidgets import QApplication

    from openkb import config
    from openkb.application.knowledge_bases import initialize_kb
    from openkb.application.pages import read_page, save_page
    from openkb.locks import atomic_write_text, kb_ingest_lock

    config.GLOBAL_CONFIG_DIR = root / "settings"
    config.GLOBAL_CONFIG_PATH = root / "settings/global.yaml"
    from openkb.desktop.window import Workbench

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("OpenKB Verification")
    for font in (Path(__file__).parents[1] / "rendering/assets/fonts").glob("*.otf"):
        QFontDatabase.addApplicationFont(str(font))
    first, other = root / "知识库 A", root / "知识库 B"
    for kb in (first, other):
        initialize_kb(
            kb,
            seed_environment=False,
            model="openai/desktop-verification" if args.model_base else None,
            api_key="verification-only" if args.model_base else None,
            openai_api_base=args.model_base,
        )
    page_path = first / "wiki/concepts/原生阅读.md"
    source = r"""---
type: Concept
description: 原生阅读验证
---
# 原生知识阅读

这是中文段落。行内公式 \(\frac{a}{b}+\sqrt{x}\) 与文字保持基线。

\[ E = mc^2 \]

```mermaid
flowchart LR
  A[导入资料] --> B[知识编译]
  B --> C[阅读与问答]
```

| 功能 | 状态 |
| --- | --- |
| Markdown 表格 | 原生显示 |
| 中文标签 | 随包字体 |

```python
print("OpenKB")
```
"""
    with kb_ingest_lock(first / ".openkb"):
        atomic_write_text(page_path, source)
    window = Workbench(history_dir=root / "tasks")
    window.show()
    evidence: dict[str, object] = {"checks": []}
    checks: list[str] = []

    def wait_until(condition, timeout=45):
        deadline = time.monotonic() + timeout
        while not condition():
            app.processEvents()
            if time.monotonic() >= deadline:
                raise TimeoutError("Native acceptance condition did not complete")
            time.sleep(0.01)
        app.processEvents()

    def saved(count):
        return len(window._seen_terminal) >= count

    try:
        environment, cwd = dict(os.environ), os.getcwd()
        window.open_knowledge_base(first)
        wait_until(lambda: window.page is not None)
        window.open_page("concepts/原生阅读")
        wait_until(lambda: window.page is not None and window.page.path == "concepts/原生阅读")
        wait_until(lambda: "与文字保持基线" in window.reader.toPlainText())
        assert "渲染失败" not in window.reader.toPlainText()
        assert "print" in window.reader.toPlainText()
        assert page_path.read_text(encoding="utf-8") == source
        window.grab().save(str(root / "native-reading.png"))
        checks.append("native Chinese, formulas, flowchart, table, code; source unchanged")

        window.editor.setPlainText(window.editor.toPlainText() + "\n第一次保存。\n")
        window._save_page()
        window.editor.setPlainText(window.editor.toPlainText() + "\n保存期间继续编辑。\n")
        wait_until(lambda: saved(1))
        window._save_page()
        wait_until(lambda: saved(2))
        assert all(task.state == "completed" for task in window.manager.tasks())
        assert "保存期间继续编辑" in read_page(first, "concepts/原生阅读").body
        wait_until(lambda: "保存期间继续编辑" in window.reader.toPlainText())
        assert read_page(first, "concepts/原生阅读").content.startswith("---\n")
        checks.append("save during continued editing retains draft and advances confirmed version")

        window.editor.setPlainText(window.editor.toPlainText() + "\n本次已保存正文。\n")
        window._save_page()
        # Deliberately delay Qt observation while another writer commits after
        # our own save. The UI must adopt its receipt, never the unseen update.
        own = window.manager.tasks()[-1]
        assert window.manager.wait(own.id, timeout=45).state == "completed"
        latest = read_page(first, "concepts/原生阅读")
        save_page(first, latest.path, latest.body + "\n另一写入者。\n", version=latest.version)
        wait_until(lambda: saved(3))
        window.editor.setPlainText(window.editor.toPlainText() + "\n本地冲突草稿。\n")
        window._save_page()
        wait_until(lambda: saved(4))
        assert window.manager.tasks()[-1].error == "conflict"
        assert "本地冲突草稿" in window.editor.toPlainText()
        assert "本地冲突草稿" not in read_page(first, latest.path).body
        checks.append("external version conflict is visible and preserves the local draft")

        window.chat.show_markdown("旧回答 $x^2$", first / "wiki")
        window.chat.show_temporary("新的流式回答")
        wait_until(window.chat.rendering_stopped)
        assert window.chat.toPlainText() == "新的流式回答"
        busy, release = threading.Event(), threading.Event()

        def hold_first_kb():
            with kb_ingest_lock(first / ".openkb"):
                busy.set()
                release.wait(45)

        holder = threading.Thread(target=hold_first_kb)
        holder.start()
        try:
            assert busy.wait(5)
            for _ in range(6):
                window._refresh()
            window.open_knowledge_base(other)
            wait_until(lambda: window.kb == other, timeout=5)
        finally:
            release.set()
            holder.join(5)
        assert not holder.is_alive()
        checks.append("busy-KB read requests do not starve navigation to an independent KB")
        assert "新的流式回答" not in window.chat.toPlainText()
        assert "原生知识阅读" not in window.reader.toPlainText()
        assert window.question.toPlainText() == ""
        checks.append(
            "superseded rendering cannot replace new text; switching KB clears old content"
        )
        from openkb.desktop.verification_settings import verify_settings

        verify_settings(window, first, wait_until)
        checks.append("native KB/global settings save, inheritance, credential rotation and clear")
        from openkb.desktop.verification_removal import verify_removal

        verify_removal(window, first, wait_until)
        checks.append(
            "native removal preview, stale confirmation, spawned cleanup and kept resources"
        )
        from openkb.desktop.verification_sessions import verify_sessions

        verify_sessions(window, first, wait_until)
        checks.append(
            "native conversation wait/stop, unique transcript copies and version-bound deletion"
        )
        from openkb.desktop.verification_maintenance import verify_diagnostics, verify_maintenance

        verify_maintenance(window, first, wait_until)
        verify_diagnostics(window, root / "待修复知识库", wait_until)
        checks.append(
            "native structural report, link repair confirmation, restricted inspection and recovery"
        )
        if args.model_base:
            from openkb.application.settings import apply_kb_config_patch
            from openkb.application.settings_data import KbConfigPatchRequest
            from openkb.desktop.verification_recompilation import verify_recompilation

            # Settings acceptance deliberately cleared the first KB credential;
            # restore only the controlled fixture for subsequent model checks.
            apply_kb_config_patch(
                first,
                KbConfigPatchRequest.model_validate(
                    {
                        "kb": str(first),
                        "api_key": "verification-only",
                        "openai_api_base": args.model_base,
                    }
                ),
            )

            verify_recompilation(window, first, wait_until)
            checks.append(
                "native selected/all recompile, skipped missing source, existing long index reused"
            )
            from openkb.desktop.verification_maintenance import verify_semantic_maintenance

            verify_semantic_maintenance(window, first, wait_until)
            checks.append("native semantic audit with real SDK requests and saved report")
            from openkb.application.conversations import read_conversation
            from openkb.application.knowledge_bases import get_kb_list
            from openkb.inputs import SUPPORTED_EXTENSIONS
            from openkb.runtime.requests import ImportFile

            if args.inputs:
                inputs = sorted(
                    p.resolve()
                    for p in args.inputs.iterdir()
                    if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
                )
                assert inputs
                task_id = window.manager.submit(other, [ImportFile(str(p)) for p in inputs])
                wait_until(lambda: task_id in window._seen_terminal, timeout=180)
                result = window.manager.get(task_id)
                assert result.state == "completed", result
                assert len(get_kb_list(other)["documents"]) == len(inputs)
                for resource in (r for unit in result.results for r in unit.resources):
                    assert Path(resource).exists(), resource
                from openkb.converter import get_pdf_page_count

                long_pdfs = [
                    p for p in inputs if p.suffix.lower() == ".pdf" and get_pdf_page_count(p) >= 20
                ]
                if long_pdfs:
                    from openkb.desktop.verification_documents import verify_long_pdf

                    for pdf in long_pdfs:
                        summary_path = verify_long_pdf(other, pdf)
                        window.open_page(summary_path)
                        wait_until(
                            lambda: window.page is not None and window.page.path == summary_path
                        )
                        wait_until(lambda: "Native compiled" in window.reader.toPlainText())
                    checks.append("real long-PDF PageIndex tree, database and all page content")
                summary = next((other / "wiki/summaries").glob("*.md"))
                window.open_page(str(summary.relative_to(other / "wiki")))
                wait_until(lambda: "Native compiled" in window.reader.toPlainText())
                checks.append("real document conversion, compilation, registry and native reading")
            if args.url:
                from PySide6.QtCore import QTimer
                from PySide6.QtWidgets import QInputDialog

                from openkb.state import HashRegistry

                def enter_urls():
                    dialog = QApplication.activeModalWidget()
                    assert isinstance(dialog, QInputDialog)
                    dialog.setTextValue("\n".join([args.url + "-missing", args.url, args.url]))
                    dialog.accept()

                QTimer.singleShot(100, enter_urls)
                window._import_urls()
                task_id = window.manager.tasks()[-1].id
                wait_until(lambda: task_id in window._seen_terminal, timeout=120)
                result = window.manager.get(task_id)
                assert (result.failed, result.succeeded, result.skipped) == (1, 1, 1), result
                assert result.processes_reaped
                entries = HashRegistry(other / ".openkb/hashes.json").all_entries()
                entry = next(m for m in entries.values() if m.get("origin") == "url")
                assert entry["path"] == args.url
                assert (other / entry["raw_path"]).is_file()
                assert (other / entry["source_path"]).is_file()
                assert result.results[0].unfinished == ("acquisition",)
                checks.append(
                    "real HTTP URL acquisition, provenance, dedup and ordinary-failure continuation"
                )
            if args.one_shot_url:
                from openkb.runtime.requests import ImportUrl

                with kb_ingest_lock(other / ".openkb"):
                    task_id = window.manager.submit(other, [ImportUrl(args.one_shot_url)])
                    wait_until(lambda: window.manager._tasks[task_id].wait_delay >= 1, timeout=45)
                    assert window.manager.get(task_id).started_at is None
                wait_until(lambda: task_id in window._seen_terminal, timeout=90)
                result = window.manager.get(task_id)
                assert result.state == "completed" and result.processes_reaped, result
                prepared_inputs = Path(window.manager._preparations.name) / task_id
                assert not list(prepared_inputs.rglob("*"))
                checks.append(
                    "one-shot HTTP PDF survives worker deferral; private input is collected"
                )
            window.mode.setCurrentIndex(0)
            window.save_answer.setChecked(True)
            window.question.setPlainText("What does this knowledge base contain?")
            window._ask()
            wait_until(lambda: window._chat_task in window._seen_terminal)
            assert window._chat_task is not None
            result = window.manager.get(window._chat_task)
            assert result.state == "completed" and result.text == "Native answer", result
            assert result.results[0].resources and Path(result.results[0].resources[0]).is_file()
            window.mode.setCurrentIndex(1)
            window.sessions.setCurrentIndex(0)
            for text in ("Start a conversation", "Continue the saved conversation"):
                window.question.setPlainText(text)
                window._ask()
                wait_until(lambda: window._chat_task in window._seen_terminal)
                assert window._chat_task is not None
                result = window.manager.get(window._chat_task)
                assert result.state == "completed", result
            session_id = result.results[0].session_id
            assert session_id is not None
            session = read_conversation(other, session_id)
            assert len(session.turns) == 2
            checks.append(
                "real SDK query/save and two persisted chat turns against local HTTP fixture"
            )
        if args.corpus:
            from openkb.desktop.verification_rendering import verify_corpus

            verify_corpus(args.corpus, root / "corpus", wait_until)
            checks.append("complete rendering corpus through Qt; visual verdict separate")
        assert os.environ == environment and os.getcwd() == cwd
        checks.append("desktop environment and cwd remain unchanged")
    finally:
        window.request_quit()
        wait_until(
            lambda: window.manager.join(0)
            and window.io.stopped()
            and window.reader.rendering_stopped()
            and window.chat.rendering_stopped()
        )
        checks.append("explicit quit reaps execution workers and rendering")
        evidence["checks"] = checks
        evidence["tasks"] = [task.summary() for task in window.manager.tasks()]
        (root / "verification.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps({"output": str(root), "checks": checks}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
