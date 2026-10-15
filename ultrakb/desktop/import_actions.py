"""Capture import ownership before selectors and route frozen requests through setup."""

from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QInputDialog

from ultrakb.inputs import SUPPORTED_EXTENSIONS
from ultrakb.runtime.requests import ImportFile, ImportUrl


def import_files(window):
    if window.kb is None or window._quitting:
        return
    root, view = window.kb, window.view_id
    remote = window.remote_assets.currentData()
    files, _ = QFileDialog.getOpenFileNames(window, "导入资料")
    if files:
        requests = tuple(
            ImportFile(str(Path(path).resolve()), view_id=view, download_remote_assets=remote)
            for path in files
        )
        window.onboarding.submit(root, requests, description=f"导入 {len(requests)} 份资料")


def import_urls(window):
    if window.kb is None or window._quitting:
        return
    root, view = window.kb, window.view_id
    value, accepted = QInputDialog.getMultiLineText(
        window, "导入网址", "每行一个 HTTP / HTTPS 地址"
    )
    if accepted and value.strip():
        try:
            requests = tuple(
                ImportUrl(line.strip(), view_id=view) for line in value.splitlines() if line.strip()
            )
            window.onboarding.submit(root, requests, description=f"导入 {len(requests)} 个网址")
        except ValueError as error:
            window._error(error)


def import_directory(window):
    if window.kb is None or window._quitting:
        return
    root, view = window.kb, window.view_id
    remote = window.remote_assets.currentData()
    path = QFileDialog.getExistingDirectory(window, "递归导入目录")
    if not path:
        return
    version = window._kb_deletion_versions.get(root, 0)

    def valid():
        return (
            not window._quitting
            and window.kb == root
            and root not in window._deleting_kbs
            and version == window._kb_deletion_versions.get(root, 0)
        )

    def loaded(files, error):
        if error:
            window._error(error)
        elif files and valid():
            requests = tuple(
                ImportFile(str(source), view_id=view, download_remote_assets=remote)
                for source in files
            )
            window.onboarding.submit(
                root, requests, description=f"从目录导入 {len(requests)} 份资料", valid=valid
            )

    window.io.submit(
        lambda: sorted(
            p.resolve()
            for p in Path(path).rglob("*")
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
        ),
        loaded,
        kb=root,
        obsolete=lambda: not valid(),
    )
