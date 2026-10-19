"""Locate an existing KB; each stored use case then validates its exact format."""

from pathlib import Path


def stored_kb_root(ctx):
    from ultrakb.cli import _require_kb_root

    if root := ctx.obj.get("kb_dir_override"):
        return root
    return next(
        (path for path in (Path.cwd(), *Path.cwd().parents) if (path / ".ultrakb").exists()),
        None,
    ) or _require_kb_root(ctx)
