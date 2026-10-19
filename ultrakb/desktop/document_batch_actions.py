"""Confirm fresh applicability, then continue exactly the reviewed regeneration selection."""

from PySide6.QtWidgets import QCheckBox, QMessageBox

from ultrakb.application.document_batches import preview_document_batch, validate_document_batch
from ultrakb.runtime.records import TaskExclusion
from ultrakb.runtime.requests import RecompileDocument, RetrySource, RetryWorksheet


def recompile(panel, identities):
    execute(panel, identities, action="recompile")


def retry(panel, identities):
    execute(panel, identities, action="retry")


def execute(panel, identities, *, action):
    if panel._task or panel._batch_preparing:
        return
    selected = tuple(identities)
    if not selected:
        panel.status.setText("请先勾选资料；行菜单只操作该行。")
        return
    panel._batch_preparing = True
    title = "重试未完成项" if action == "retry" else "重新生成知识"
    panel.status.setText("正在重新核对所选资料的适用范围…")

    def loaded(preview, error):
        panel._batch_preparing = False
        if error:
            panel.status.setText(str(error))
            return
        targets = preview.retry_targets if action == "retry" else preview.targets
        lines = [f"适用：{target.name}" for target in targets]
        lines.extend(f"不执行：{skip.name} · {skip.reason}" for skip in preview.skipped)
        panel.details.setPlainText("\n".join(lines))
        panel.details.show()
        if not preview.approved:
            panel.status.setText("所选资料当前均不适用；请查看逐项原因。")
            return
        question = QMessageBox(panel)
        question.setWindowTitle(f"确认所选资料{title}")
        question.setText(
            f"目标：{preview.kb_dir}\n选中 {len(preview.selected)} 项，"
            f"仅执行 {len(preview.approved)} 项；{len(preview.skipped)} 项不执行。"
        )
        question.setInformativeText(
            f"共 {len(targets)} 个处理单元。"
            + (
                "仅重试未完成内容；已完成工作表保留。单项失败继续，模型共享故障停止本批余项。"
                if action == "retry"
                else "复用保存的正文和索引；人工修改保留为待接受差异。"
            )
            + "每项完成后保留结果，停止不会撤销已完成项。"
        )
        summary_retry = None
        if action == "retry" and any(target.failed_summary for target in targets):
            summary_retry = QCheckBox("重试失败摘要（额外调用模型，计入原任务预算）")
            summary_retry.setToolTip(
                "复用已有索引和通过校验的摘要批次；仅为失败摘要开放一次恢复机会。"
                "默认不勾选；再次失败会保留诊断，不会自动重跑整份资料。"
            )
            question.setCheckBox(summary_retry)
            question.setInformativeText(
                question.informativeText()
                + "已发现失败摘要。勾选下方选项可复用索引和已通过的摘要批次，"
                "仅对失败摘要额外尝试一次，调用计入原任务预算。"
            )
        question.setDetailedText("\n".join(lines))
        question.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        question.setDefaultButton(QMessageBox.StandardButton.No)
        if question.exec() != QMessageBox.StandardButton.Yes:
            return
        retry_failed_response = bool(summary_retry and summary_retry.isChecked())

        def submit_reviewed():
            # The wizard may have been open for hours; UI eligibility is never
            # execution authorization. Workers also recheck the version under lease.
            panel._batch_preparing = True

            def submitted(task_id, count):
                panel._batch_preparing = False
                panel._task = task_id
                panel.status.setText(f"已提交 {count} 项；可在任务页查看逐项结果与停止。")
                from ultrakb.desktop.document_selection import update

                update(panel)

            def failed(error):
                panel._batch_preparing = False
                panel.status.setText(str(error))

            submit_reviewed_batch(
                panel.window,
                preview,
                panel.scope,
                submitted,
                failed,
                obsolete=lambda: panel._closed or panel.window.kb != preview.kb_dir,
                retry_failed_response=retry_failed_response,
            )

        panel.window.onboarding.ensure(
            preview.kb_dir,
            f"所选 {len(preview.approved)} 项{title}",
            submit_reviewed,
            valid=lambda: not panel._closed,
        )

    panel.window.io.submit(
        lambda: preview_document_batch(panel.kb, selected, scope=panel.scope, action=action),
        loaded,
        kb=panel.kb,
        obsolete=lambda: panel._closed,
    )


def requests_for(preview, view_id, *, retry_failed_response=False):
    if preview.action == "retry":
        return tuple(
            RetryWorksheet(
                target.identity.value,
                target.unit_id,
                source_revision_id=target.source_revision_id,
                unit_revision_id=target.unit_revision_id,
                view_id=view_id,
                retry_failed_response=retry_failed_response and target.failed_summary,
            )
            if target.worksheet
            else RetrySource(
                target.identity.value,
                source_revision_id=target.source_revision_id,
                unit_revision_id=target.unit_revision_id,
                view_id=view_id,
                retry_failed_response=retry_failed_response and target.failed_summary,
            )
            for target in preview.retry_targets
        )
    assert preview.version is not None
    return tuple(
        RecompileDocument(
            target.file_hash, preview.version, view_id=view_id, unit_id=target.unit_id
        )
        for target in preview.targets
    )


def submit_reviewed_batch(
    window, preview, scope, submitted, failed, *, obsolete, retry_failed_response=False
):
    """Both whole-KB and selected batches retain the same fresh targets and exclusions."""

    def validated(current, error):
        if error:
            failed(error)
            return
        requests = requests_for(
            current, scope.view_id if scope else None, retry_failed_response=retry_failed_response
        )
        try:
            task_id = window.manager.submit(
                current.kb_dir,
                requests,
                require_verified_connection=True,
                not_applicable=tuple(
                    TaskExclusion(skip.identity.kind, skip.identity.value, skip.name, skip.reason)
                    for skip in current.skipped
                ),
            )
        except Exception as error:
            failed(error)
            return
        submitted(task_id, len(requests))

    window.io.submit(
        lambda: validate_document_batch(preview, scope=scope),
        validated,
        kb=preview.kb_dir,
        obsolete=lambda: window._quitting or obsolete(),
    )
