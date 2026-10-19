"""The resident UI offers idle runtime slots to the shared durable dispatcher."""

import logging

from PySide6.QtCore import QObject, QTimer

from ultrakb.application.pending import claim_pending_job
from ultrakb.runtime.requests import RunPendingJob


class PendingDispatcher(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.roots = []
        self.busy = False
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(1000)

    def watch(self, root):
        if root not in self.roots:
            self.roots.append(root)

    def poll(self):
        if self.busy or self.window._quitting:
            return
        for root in tuple(self.roots):
            if root in self.window._deleting_kbs or self.window.manager.has_work(root):
                continue
            self.roots.remove(root)
            self.roots.append(root)
            self.busy = True
            selected_scope = getattr(self.window, "read_scope", None)
            if selected_scope is not None and (
                selected_scope.knowledge is None or selected_scope.kb_dir != root
            ):
                selected_scope = None

            def ready(selected, error, root=root, selected_scope=selected_scope):
                self.busy = False
                if error:
                    logging.getLogger(__name__).warning("Pending dispatcher: %s", error)
                elif (
                    selected and not self.window._quitting and root not in self.window._deleting_kbs
                ):
                    self.window.manager.submit(
                        root,
                        [
                            RunPendingJob(
                                selected["id"],
                                selected["dispatch_id"],
                                view_id=selected_scope.view_id if selected_scope else None,
                            )
                        ],
                    )

            self.window.io.submit(
                lambda root=root, selected_scope=selected_scope: claim_pending_job(
                    root, scope=selected_scope, cancelled=lambda: self.window._quitting
                ),
                ready,
                kb=None if selected_scope else root,
                exclusive=True,
            )
            break
