"""Keep whole-KB maintenance in Knowledge/Tasks, using shared command boundaries."""

from PySide6.QtWidgets import QMessageBox, QToolButton

from ultrakb.application.document_actions import document_inventory
from ultrakb.application.document_batches import preview_document_batch
from ultrakb.desktop.document_batch_actions import submit_reviewed_batch


def recompile_all(window):
    root, scope = window.kb, window.scope
    if root is None:
        return

    def loaded(preview, error):
        if error:
            window._error(error)
            return
        if not preview.approved:
            window.statusBar().showMessage("暂无可重新生成知识的资料。")
            return
        question = QMessageBox(window)
        question.setWindowTitle("全库重新生成知识")
        question.setText(
            f"目标：{root}\n全库 {len(preview.selected)} 项，仅执行 {len(preview.approved)} 项；"
            f"{len(preview.skipped)} 项不执行。"
        )
        question.setInformativeText(
            "复用保存的正文和索引；人工修改保留为待接受差异。已完成项保留，停止不会撤销。"
        )
        question.setDetailedText(
            "\n".join(
                [f"适用：{target.name}" for target in preview.targets]
                + [f"不执行：{skip.name} · {skip.reason}" for skip in preview.skipped]
            )
        )
        question.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        question.setDefaultButton(QMessageBox.StandardButton.No)
        if question.exec() == QMessageBox.StandardButton.Yes:

            def submit_reviewed():
                submit_reviewed_batch(
                    window,
                    preview,
                    scope,
                    lambda task_id, count: window.statusBar().showMessage(
                        f"已提交 {count} 项全库资料重新生成任务。"
                    ),
                    window._error,
                    obsolete=lambda: window.kb != root,
                )

            window.onboarding.ensure(root, "全库适用资料重新生成知识", submit_reviewed)

    window.io.submit(
        lambda: preview_document_batch(
            root, tuple(row.identity for row in document_inventory(root, scope=scope)), scope=scope
        ),
        loaded,
        kb=root,
        obsolete=lambda: window._quitting,
    )


def maintenance_menu(window, *, tasks=False):
    from PySide6.QtWidgets import QMenu

    from ultrakb.desktop.pending import PendingDialog
    from ultrakb.desktop.proposals import ProposalsDialog
    from ultrakb.desktop.refresh import RefreshDialog

    button = QToolButton()
    button.setText("待处理工作" if tasks else "知识维护")
    button.setAccessibleName(button.text())
    button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    menu = QMenu(button)
    if tasks:
        menu.addAction(
            "查看待处理工作…",
            lambda: PendingDialog(window, window.kb).show() if window.kb else None,
        )
    else:
        menu.addAction(
            "更新知识与历史依据…",
            lambda: RefreshDialog(window, window.kb).show() if window.kb else None,
        )
        menu.addAction(
            "查看待接受差异…",
            lambda: ProposalsDialog(window, window.kb).show() if window.kb else None,
        )
        menu.addAction("全库重新生成知识…", lambda: recompile_all(window))
    button.setMenu(menu)
    return button
