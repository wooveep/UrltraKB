"""The resident UI offers idle runtime slots to the shared durable dispatcher."""

import logging

from PySide6.QtCore import QObject, QTimer

from openkb.application.pending import claim_pending_job
from openkb.runtime.requests import RunPendingJob


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

            def ready(selected, error, root=root):
                self.busy = False
                if error:
                    logging.getLogger(__name__).warning("Pending dispatcher: %s", error)
                elif (
                    selected and not self.window._quitting and root not in self.window._deleting_kbs
                ):
                    self.window.manager.submit(
                        root, [RunPendingJob(selected["id"], selected["dispatch_id"])]
                    )

            self.window.io.submit(
                lambda root=root: claim_pending_job(root), ready, kb=root, exclusive=True
            )
            break
