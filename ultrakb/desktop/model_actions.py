"""Route panel commands through the same native setup, preserving their targets."""

from ultrakb.runtime.requests import requires_model


def submit_panel(panel, requests, *, description, on_submitted=None):
    units = tuple(requests)

    def submitted(task):
        if panel._closed:
            return
        if on_submitted:
            on_submitted(task)
        else:
            panel._task = task
            panel.status.setText(f"{description}已提交，可查看任务进度与结果。")

    if any(requires_model(unit) for unit in units):
        panel.status.setText(f"{description}：正在检查连接；原操作已保留。")
        panel.window.onboarding.submit(
            panel.kb,
            units,
            description=description,
            on_submitted=submitted,
            valid=lambda: not panel._closed,
        )
    else:
        submitted(panel.window.manager.submit(panel.kb, units))
