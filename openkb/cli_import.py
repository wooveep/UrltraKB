"""CLI presentation of the shared isolated document task runtime."""

from __future__ import annotations

import time
from pathlib import Path

import click

from openkb.inputs import SUPPORTED_EXTENSIONS
from openkb.runtime.records import TERMINAL
from openkb.runtime.requests import ImportFile, ImportUrl, UnitRequest
from openkb.runtime.tasks import TaskManager
from openkb.url_ingest import looks_like_url


def import_path(kb_dir: Path, path: str) -> int:
    from openkb.config import GLOBAL_CONFIG_DIR

    requests: list[UnitRequest]
    if looks_like_url(path):
        requests = [ImportUrl(path)]
    else:
        target = Path(path).expanduser().resolve()
        if not target.exists():
            raise click.BadParameter(f"Path does not exist: {path}")
        if target.is_file() and target.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise click.BadParameter(f"Unsupported file type: {target.suffix}")
        files = sorted(target.rglob("*")) if target.is_dir() else [target]
        files = [
            file for file in files if file.is_file() and file.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
        if not files:
            raise click.BadParameter("No supported documents found")
        requests = [ImportFile(str(file)) for file in files]
    manager = TaskManager(history_dir=GLOBAL_CONFIG_DIR / "cli/tasks")
    previous = None
    interrupted = False
    try:
        task_id = manager.submit(kb_dir, requests)
        click.echo(f"Task: {task_id} · {len(requests)} document(s)")
        while True:
            try:
                view = manager.get(task_id)
                if view.stage != previous:
                    click.echo(f"  {view.stage} ({len(view.results)}/{view.total})")
                    previous = view.stage
                if view.state in TERMINAL and view.processes_reaped:
                    break
                time.sleep(0.1)
            except KeyboardInterrupt:
                interrupted = True
                manager.stop(task_id)
                click.echo("Stopping; waiting for execution and recovery confirmation…")
        for result in view.results:
            document = result.document
            if document:
                click.echo(
                    f"  [{document.status.upper()}] {Path(document.source).name}: "
                    f"intake={document.source_intake}, "
                    f"compilation={document.knowledge_compilation}"
                )
                if document.reason:
                    click.echo(f"    {document.stage}: {document.reason}")
            elif result.error:
                click.echo(f"  {result.error}")
        if view.error:
            click.echo(view.error)
        if interrupted or view.state == "stopped":
            return 130
        return 0 if view.state == "completed" else 1
    finally:
        manager.shutdown(stop=True)
        while not manager.join(1):
            pass
