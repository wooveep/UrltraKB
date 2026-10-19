"""Path-based local KB discovery, including root children and registry ghosts."""

from pathlib import Path

from ultrakb import config
from ultrakb.lifecycle import registered_path


def knowledge_bases() -> list[tuple[str, Path]]:
    """Keep colliding display names: the desktop always identifies KBs by path."""
    with config._with_global_config_lock():
        values = config.load_global_config()
        root = config.kb_root_dir()
        found: dict[Path, str] = {}
        if root.is_dir():
            for child in sorted(root.iterdir()):
                if config._is_kb_dir(child):
                    found.setdefault(registered_path(child), child.name)
        aliases = values.get("kb_aliases", {})
        if isinstance(aliases, dict):
            for name, raw in aliases.items():
                if isinstance(name, str) and isinstance(raw, str):
                    found.setdefault(registered_path(Path(raw)), name)
        known = values.get("known_kbs", [])
        if isinstance(known, list):
            for raw in known:
                if isinstance(raw, str):
                    path = registered_path(Path(raw))
                    found.setdefault(path, path.name)
        return [(name, path) for path, name in found.items()]


def knowledge_base_overview(kb_dir: Path) -> dict:
    """Count committed resources and managed originals from one bound publication."""
    from ultrakb.application.knowledge_bases import get_kb_status
    from ultrakb.knowledge_scope import resolve_scope

    return get_kb_status(kb_dir, scope=resolve_scope(kb_dir))
