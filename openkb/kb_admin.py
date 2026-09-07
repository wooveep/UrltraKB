"""Knowledge-base admin operations: physically delete a KB + registry cleanup.

Kept out of config.py (which sits near the per-file line gate) because these
mutate the filesystem (``shutil.rmtree`` under the KB ingest lock) and the
global registry — side effects the otherwise-pure config-loading module avoids.

Imports the config MODULE (not names) so ``config.GLOBAL_CONFIG_PATH`` is read
at call time — a test that monkeypatches it (isolated global.yaml) must be
honored, which a by-value ``from openkb.config import GLOBAL_CONFIG_PATH`` would
silently break.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from openkb import config
from openkb.locks import kb_ingest_lock


def unregister_kb(kb_path: Path) -> None:
    """Remove ``kb_path`` from the global registry (its ``known_kbs`` entry, any
    ``kb_aliases`` pointing at it, ``default_kb`` if it referenced it). Held
    under the global-config lock; a no-op for a never-registered path."""
    resolved = str(kb_path.resolve())
    with config._with_global_config_lock():
        gc = config._load_global_config_unlocked()
        changed = False
        known = gc.get("known_kbs")
        if isinstance(known, list):
            pruned = [k for k in known if k != resolved]
            if len(pruned) != len(known):
                gc["known_kbs"] = pruned
                changed = True
        aliases = gc.get("kb_aliases")
        if isinstance(aliases, dict):
            pruned_aliases = {a: p for a, p in aliases.items() if p != resolved}
            if len(pruned_aliases) != len(aliases):
                gc["kb_aliases"] = pruned_aliases
                changed = True
        if gc.get("default_kb") == resolved:
            del gc["default_kb"]
            changed = True
        if changed:
            config._atomic_yaml_dump(config.GLOBAL_CONFIG_PATH, gc)


def resolve_deletion_alias(name: str) -> Path:
    """Use the existing name policy, validating its original registered path."""
    import os

    from openkb.lifecycle import deletion_target, has_pending_deletion

    name = config.validate_kb_name(name)
    with config._with_global_config_lock():
        values = config.load_global_config()
        target = config.resolve_kb_alias(name)

        def validated(path: Path) -> Path:
            resolved = deletion_target(path)
            if resolved != target:
                raise ValueError("Knowledge-base path changed during name resolution")
            return resolved

        aliases = values.get("kb_aliases", {})
        alias = aliases.get(name) if isinstance(aliases, dict) else None
        if isinstance(alias, str):
            return validated(Path(alias))
        raw_root = os.environ.get("OPENKB_KB_ROOT") or values.get("kb_root")
        if not isinstance(raw_root, str) or not raw_root.strip():
            raw_root = str(config.GLOBAL_CONFIG_DIR / "kbs")
        candidate = Path(raw_root).expanduser() / name
        if candidate.resolve() == target:
            return validated(candidate)
        if has_pending_deletion(candidate):
            raise ValueError(
                f"Unfinished deletion at {candidate}; the name now identifies another KB. "
                "Select the original full path in desktop knowledge-base management."
            )
        known = values.get("known_kbs", [])
        if isinstance(known, list):
            for raw in known:
                if isinstance(raw, str) and Path(raw).expanduser().resolve() == target:
                    return validated(Path(raw))
        return validated(target)


def _remember_pending_deletion(kb_dir: Path, **wait_options) -> None:
    """A root-discovered KB must stay discoverable if removal loses its wiki tree."""
    with config._with_global_config_lock(**wait_options):
        values = config._load_global_config_unlocked()
        known = values.get("known_kbs", [])
        if not isinstance(known, list):
            raise ValueError("Invalid knowledge-base registry; repair global settings first")
        if str(kb_dir) not in known:
            values["known_kbs"] = [*known, str(kb_dir)]
            config._atomic_yaml_dump(config.GLOBAL_CONFIG_PATH, values)


def delete_kb(kb_dir: Path, *, generation: str | None = None, cancelled=None, on_wait=None) -> None:
    """Physically delete a KB directory and unregister it (irreversible; the
    CALLER confirms). An existing ``kb_dir`` MUST be a KB (``.openkb`` + ``wiki``)
    or :class:`ValueError` is raised and nothing removed; a ghost entry
    (directory already gone) is tolerated — no ``rmtree``, just unregistered.
    """
    import uuid

    from openkb.lifecycle import (
        LifecycleState,
        deletion_binding,
        deletion_target,
        directory_identity,
        exclusive_lifecycle,
        read_state,
        write_state,
    )

    requested = kb_dir
    kb_dir = deletion_target(requested)
    expected_directory = directory_identity(kb_dir)
    expected_generation = read_state(kb_dir).generation
    with exclusive_lifecycle(kb_dir, cancelled=cancelled, on_wait=on_wait):
        previous = read_state(kb_dir)
        if (
            deletion_target(requested) != kb_dir
            or directory_identity(kb_dir) != expected_directory
            or previous.generation != expected_generation
            or (generation is not None and deletion_binding(kb_dir) != generation)
        ):
            raise ValueError("Refusing to delete a replaced directory; review the current target")
        exists = kb_dir.exists()
        if previous.status == "deleting":
            if exists and directory_identity(kb_dir) != previous.directory:
                raise ValueError("Refusing to resume deletion of a replaced directory")
        elif exists:
            if not config._is_kb_dir(kb_dir):
                raise ValueError(f"Refusing to delete: not a knowledge base directory: {kb_dir}")
            with kb_ingest_lock(kb_dir / ".openkb"):
                pass  # Recover while the internal handle can still be opened.
        if exists or previous.status == "deleting":
            _remember_pending_deletion(kb_dir, cancelled=cancelled, on_wait=on_wait)
        state = LifecycleState(uuid.uuid4().hex, "deleting", directory_identity(kb_dir))
        write_state(kb_dir, state)  # Invalidate queued work BEFORE removing any files.
        if exists:
            shutil.rmtree(kb_dir)
        write_state(kb_dir, LifecycleState(state.generation, "absent"))
        unregister_kb(kb_dir)
