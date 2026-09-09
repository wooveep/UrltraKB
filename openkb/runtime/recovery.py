"""Recover a terminated document owner in another supervised process."""

from __future__ import annotations

import time
from dataclasses import asdict
from pathlib import Path

from openkb.runtime.records import UnitIdentity


def recover_worker(identity: UnitIdentity, connection, timeout: float) -> None:
    from openkb.locks import kb_ingest_lock

    try:
        # Acquiring the existing write lease drains pending journals and checks
        # the repair gate before any later document is allowed to modify the KB.
        with kb_ingest_lock(Path(identity.kb_dir) / ".openkb", deadline=time.monotonic() + timeout):
            pass
        connection.send({"identity": asdict(identity), "kind": "recovered"})
    except BaseException:
        connection.send({"identity": asdict(identity), "kind": "recovery-failed"})
    finally:
        connection.close()
