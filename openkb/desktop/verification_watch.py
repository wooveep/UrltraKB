"""Actual Qt watch controls and spawned document tasks against the HTTP fixture."""


def verify_watch(window, kb, wait_until, *, model=False):
    from openkb.desktop.watch import WatchDialog
    from openkb.locks import atomic_write_text, kb_ingest_lock
    from openkb.state import HashRegistry

    source = kb / "raw/监听启动.md"
    with kb_ingest_lock(kb / ".openkb"):
        atomic_write_text(source, "# 监听启动\n\n这份稳定资料来自启动补查。")
        if not model:
            registry = HashRegistry(kb / ".openkb/hashes.json")
            registry.add(
                registry.hash_file(source), {"name": source.name, "raw_path": "raw/监听启动.md"}
            )
    before = {task.id for task in window.manager.tasks()}
    dialog = WatchDialog(window.watch_registry, kb, window)
    dialog.show()
    try:
        dialog.start_button.click()
        watch = window.watch_registry.start(kb / ".")
        assert len(window.watch_registry.watches()) == 1
        wait_until(lambda: watch.view().scans > 0 and watch.view().pending == 0)
        if model:
            wait_until(lambda: watch.view().submitted == 1 and watch.view().active == 0)
            first = next(task for task in window.manager.tasks() if task.id not in before)
            assert first.succeeded == 1 and first.results[0].revision, first
            nested = kb / "raw/中文 空格"
            nested.mkdir()
            temporary = nested / ".pending"
            temporary.write_text("# 原子保存\n\n另一份完整资料。", encoding="utf-8")
            temporary.rename(nested / "原子保存.md")
            wait_until(lambda: watch.view().submitted == 2 and watch.view().active == 0)
            tasks = [task for task in window.manager.tasks() if task.id not in before]
            assert len(tasks) == 2 and all(task.succeeded == 1 for task in tasks), tasks
        else:
            assert watch.view().submitted == 0
        dialog.refresh()
        dialog.grab().save(str(kb.parent / "native-watch.png"))
        dialog.table.selectRow(0)
        dialog.stop_button.click()
        wait_until(lambda: watch.join(0))
        assert watch.view().state == "stopped"
        count = len(window.manager.tasks())
        (kb / "raw/停止之后.md").write_text("No late task", encoding="utf-8")
        assert len(window.manager.tasks()) == count
    finally:
        dialog.reject()
        window.watch_registry.stop_all(close=False)
        wait_until(window.watch_registry.stopped)
