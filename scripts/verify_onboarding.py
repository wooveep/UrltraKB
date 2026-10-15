"""Exercise empty-KB setup, cancellation and immutable continuation on real Qt."""

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
    parser.add_argument("--real", action="store_true")
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    os.environ["XDG_CONFIG_HOME"] = str(root / "profile")
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "true"
    os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
    for key in (
        "LLM_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENAI_API_BASE",
        "ULTRAKB_DIR",
    ):
        os.environ.pop(key, None)
    if args.headless:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtWidgets import QDialog

    from ultrakb import config
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.desktop.verification import create_application
    from ultrakb.desktop.window import Workbench

    config.GLOBAL_CONFIG_DIR = root / "settings"
    config.GLOBAL_CONFIG_PATH = config.GLOBAL_CONFIG_DIR / "global.yaml"
    first, other = root / "知识库 A", root / "知识库 B"
    for kb in (first, other):
        initialize_kb(kb, seed_environment=False)
    source = root / "original.md"
    source.write_text("# Original input\n\nThis file must survive setup cancellation.\n")
    app = create_application()
    app.setQuitOnLastWindowClosed(False)
    assert args.headless or app.platformName() not in {"offscreen", "minimal"}
    window = Workbench(history_dir=root / "tasks")
    window.resize(1366, 768)
    window.show()
    accepted, checks = [], []
    live = boundary = None

    def wait(condition, timeout=30):
        deadline = time.monotonic() + timeout
        while not condition():
            app.processEvents()
            time.sleep(0.01)
            if time.monotonic() > deadline:
                raise TimeoutError("Native onboarding acceptance timed out")
        app.processEvents()

    def open_kb(kb):
        window.open_knowledge_base(kb)
        wait(lambda: window.kb == kb and not window.io._callbacks)

    def original_action():
        accepted.append((str(first), str(source)))

    try:
        open_kb(first)
        assert not window.manager.tasks() and source.exists()
        checks.append("A01: unconfigured empty KB can be created and browsed without a task")
        window.onboarding.ensure(first, "继续导入 original.md", original_action)
        wait(lambda: window.onboarding.dialog is not None)
        first_dialog = window.onboarding.dialog
        wait(lambda: first_dialog.connection.view is not None)
        first_dialog.grab().save(str(root / "a01-setup.png"))
        window.onboarding.ensure(first, "继续导入 original.md", original_action)
        assert window.onboarding.dialog is first_dialog and not accepted
        first_dialog.reject()
        assert window.onboarding.pending.root == first and not accepted
        assert (first / ".ultrakb/config.yaml").exists() and source.exists()
        checks.append(
            "A02: later/cancel retains original input, target, empty KB and saved configuration"
        )

        window.onboarding.resume()
        dialog = window.onboarding.dialog
        open_kb(other)
        assert window.onboarding.dialog is None and not accepted
        # A retired/late success cannot submit into either the new current KB or the old one.
        window.onboarding.completed(dialog)
        assert not accepted and window.onboarding.pending.root == first
        checks.append("A14: switching KB retires the guide; late success does not submit")
        open_kb(first)
        window.onboarding.resume()
        dialog = window.onboarding.dialog
        editor = dialog.connection
        wait(lambda: editor.view is not None)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
        from ultrakb.application.connection_capacity import template_capacity
        from ultrakb.application.connection_templates import CONNECTION_TEMPLATES
        from ultrakb.application.settings import apply_global_config_patch, apply_kb_config_patch
        from ultrakb.application.settings_data import GlobalConfigPatchRequest, KbConfigPatchRequest

        model = "deepseek/deepseek-flash"
        capacity = template_capacity(CONNECTION_TEMPLATES[2])
        if args.real:
            from live_llm import LiveLLM
            from llm_test_budget import TestRunBudget

            live = LiveLLM(TestRunBudget(root / "cost.json"))
            credentials = live.credentials
            editor.service.setCurrentIndex(2)
            editor.base_url.setText(credentials.base_url)
            editor.api_key.setText(credentials.api_key)
            editor.model.setEditText(model)
            editor.set_step(2)
            dialog.primary.click()
        else:
            from error_transport import ErrorTransport
            from test_model_connections import retained_connection_proof

            from ultrakb.application.model_connections import capture_connection
            from ultrakb.config import LlmCredentialBundle

            boundary = ErrorTransport(status=401)
            credentials = LlmCredentialBundle(api_key="fixture-private-key", base_url=boundary.url)
            # The offline scenario starts from persisted verification domain input.
            # Reopen the guide so its actual LocalIO load reads that old proof.
            dialog.reject()
            changes = {"model": model, "model_capacity": capacity}
            apply_global_config_patch(
                GlobalConfigPatchRequest(
                    config=changes,
                    api_key=credentials.api_key,
                    openai_api_base=credentials.base_url,
                )
            )
            apply_kb_config_patch(first, KbConfigPatchRequest(kb=str(first), config=changes))
            assert not capture_connection(first).missing
            retained_connection_proof(first)
            window.onboarding.resume()
            dialog = window.onboarding.dialog
            wait(lambda: dialog.connection.state is not None)
            assert dialog.connection.state.state == "passed"
            dialog.connection.set_step(2)
            dialog.primary.click()
        wait(lambda: len(accepted) == 1)
        assert accepted == [(str(first), str(source))]
        assert window.onboarding.pending is None and window.onboarding.dialog is None
        window.onboarding.completed(dialog)
        assert len(accepted) == 1
        checks.append(
            "A01/A02: continuing the verified connection resumes the captured action exactly once "
            "after explicit overview resume"
        )
        assert not any(w.isVisible() for w in app.topLevelWidgets() if isinstance(w, QDialog))
        apply_global_config_patch(
            GlobalConfigPatchRequest(
                config={"model": model, "model_capacity": capacity},
                api_key=credentials.api_key,
                openai_api_base=credentials.base_url,
            )
        )
        open_kb(other)
        wait(lambda: window.onboarding.ready.get(other))
        immediate = []
        window.onboarding.ensure(other, "已验证连接的操作", lambda: immediate.append(True))
        wait(lambda: immediate == [True])
        assert immediate == [True] and window.onboarding.dialog is None
        checks.append("A03: another inheriting KB reuses verification without another test")
        # Dispose the originating panel while the asynchronous preflight is pending.
        window.onboarding.ready[other] = False
        owner = {"closed": False}
        window.onboarding.ensure(
            other,
            "已关闭面板的操作",
            lambda: immediate.append(False),
            valid=lambda: not owner["closed"],
        )
        owner["closed"] = True
        wait(lambda: window.onboarding.pending is None)
        assert immediate == [True] and not window.manager.tasks()
        checks.append("Disposed panel: delayed preflight cannot submit the retired operation")
        if boundary:
            assert boundary.requests == []
        window.grab().save(str(root / "overview-ready.png"))
        report = {
            "platform": app.platformName(),
            "headless": args.headless,
            "checks": checks,
            "service": "real official DeepSeek" if live else "retained verification domain input",
            "verification_mode": "real-flash" if live else "retained-history",
            "model_probe_requests": len(live._proxy.requests) if live else len(boundary.requests),
            "continued_action": "captured callback only; complete import/Q&A flow belongs to #161",
        }
        if live:
            report["cost"] = live.budget.report()
    finally:
        if window.onboarding.dialog:
            window.onboarding.dialog.reject()
        window.request_quit()
        wait(window.shutdown_complete)
        if boundary:
            boundary.close()
        if live:
            live.close()
    (root / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
