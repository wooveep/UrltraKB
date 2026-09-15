"""Read settings independently of long-running wiki mutations.

KB settings saves and journal recovery hold the global configuration lock.
Readers keep the directory's lifecycle lease, then take that short config lock.
They only need the KB execution lease to recover an interrupted settings write.
"""

from __future__ import annotations

import json
from contextlib import contextmanager, nullcontext
from pathlib import Path

from openkb.config import _with_global_config_lock
from openkb.lifecycle import read_lifecycle, validate_execution
from openkb.locks import kb_ingest_lock_held, kb_read_lock
from openkb.mutation import RecoveryRequired, repair_marker


def _pending_settings_recovery(kb: Path) -> bool:
    """Ignore active document journals; uncertain journals require normal recovery."""
    targets = (kb / ".openkb/config.yaml", kb / ".env")
    for path in (kb / ".openkb/journal").glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (
                data["kb_dir"] != str(kb)
                or data["version"] != 1
                or data["status"] not in {"active", "recovering", "committed", "rolled_back"}
                or not isinstance(data["entries"], list)
            ):
                return True
            for entry in data["entries"]:
                target = Path(entry["target"])
                if not target.is_relative_to(kb) or ".." in target.parts:
                    return True
                if any(settings == target or target in settings.parents for settings in targets):
                    return True
        except FileNotFoundError:
            continue  # A document task committed and removed its journal.
        except (ValueError, KeyError, TypeError):
            return True
    return False


@contextmanager
def settings_read_lock(kb: Path | None = None, **wait_options):
    root = kb.expanduser().resolve() if kb is not None else None
    with read_lifecycle(root, **wait_options) if root else nullcontext():
        while True:
            with _with_global_config_lock(**wait_options):
                if root is not None and repair_marker(root).exists():
                    raise RecoveryRequired(f"Knowledge base needs repair: {root}")
                if (
                    root is None
                    or kb_ingest_lock_held(root / ".openkb")
                    or not _pending_settings_recovery(root)
                ):
                    if root is not None:
                        validate_execution(root)
                    yield
                    return
            # Release the config lock BEFORE waiting for the KB lease: the
            # current task may still need global settings to finish execution.
            assert root is not None
            with kb_read_lock(root / ".openkb", **wait_options):
                pass
