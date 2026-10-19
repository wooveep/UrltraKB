"""The three development adapters expose the same retained import-review result."""

import json
import subprocess
import sys

import pytest
from stream_transport import StreamTransport
from test_storage_import_proposals import compiler_responses, reviewed_import

from ultrakb.application.views import storage_scope
from ultrakb.storage.wiki_projection import ProjectionStore

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["cli", "http", "native"])
async def test_import_acceptance_adapters_report_publication_separately_from_projection(
    tmp_path, monkeypatch, adapter
):
    with StreamTransport(compiler_responses()) as service:
        root, identity, _, scope, _, _, proposal = await reviewed_import(tmp_path, service)
        (root / ".env").unlink()

        def unavailable(*args, **kwargs):
            raise OSError("The working copy is unavailable")

        monkeypatch.setattr(ProjectionStore, "sync", unavailable)
        if adapter == "cli":
            from click.testing import CliRunner

            import ultrakb.cli as cli

            monkeypatch.setattr(cli, "_find_kb_dir", lambda *args: root)
            monkeypatch.setattr(cli, "_selected_scope", lambda *args, **kwargs: scope)
            shown = CliRunner().invoke(cli.cli, ["proposals", "show", proposal.proposal_id])
            assert proposal.version in shown.output and "human annotation" in shown.output
            result = CliRunner().invoke(
                cli.cli,
                ["proposals", "accept", proposal.proposal_id, "--version", proposal.version],
            )
            assert result.exit_code == 0, result.output
            assert "Wiki synchronization pending" in result.output
            assert storage_scope(root, identity).publication_id in result.output
        elif adapter == "http":
            from fastapi.testclient import TestClient

            import ultrakb.api_documents_router as documents
            import ultrakb.api_views as views
            from ultrakb.api import create_app

            monkeypatch.delenv("ULTRAKB_API_TOKEN", raising=False)
            monkeypatch.setattr(documents, "_resolve_kb", lambda *args: root)
            monkeypatch.setattr(views, "view_scope", lambda *args, **kwargs: scope)
            with TestClient(create_app()) as client:
                response = client.post(
                    "/api/v1/proposal/accept",
                    json={
                        "kb": str(root),
                        "view_id": "legacy",
                        "proposal_id": proposal.proposal_id,
                        "version": proposal.version,
                    },
                )
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["status"] == "added" and result["projection_pending"]
            assert result["publication_id"] == storage_scope(root, identity).publication_id
            assert "synchronize wiki" in result["unfinished"]
        else:
            script = tmp_path / "import_review_worker.py"
            script.write_text("""import json, multiprocessing, sys
from pathlib import Path
from uuid import uuid4

def run(directory, proposal, version, queue):
    from ultrakb.application.execution import ExecutionContext
    import ultrakb.application.views as views
    from ultrakb.storage.database import open_database
    from ultrakb.runtime.records import UnitIdentity
    from ultrakb.runtime.requests import AcceptProposal
    from ultrakb.runtime.worker import _execute
    from ultrakb.storage.wiki_projection import ProjectionStore
    root = Path(directory)
    with open_database(root, read_only=True) as db:
        identity = db.identity
    scope = views.storage_scope(root, identity)
    views.view_scope = lambda *args, **kwargs: scope
    def unavailable(*args, **kwargs):
        raise OSError("The working copy is unavailable")
    ProjectionStore.sync = unavailable
    request = AcceptProposal(proposal, version, view_id="legacy")
    worker = UnitIdentity.create(uuid4().hex, 0, str(root), request, generation=identity.generation)
    result = _execute(request, worker, ExecutionContext())
    queue.put({"status": result.status, "revision": result.revision,
               "unfinished": result.unfinished, "resources": result.resources})

if __name__ == "__main__":
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    child = context.Process(target=run, args=(*sys.argv[1:], queue))
    child.start()
    result = queue.get(timeout=30)
    child.join(timeout=5)
    assert child.exitcode == 0
    print(json.dumps(result))
""")
            process = subprocess.run(
                [sys.executable, str(script), str(root), proposal.proposal_id, proposal.version],
                capture_output=True,
                text=True,
                timeout=45,
            )
            assert process.returncode == 0, process.stderr[-2000:]
            result = json.loads(process.stdout.splitlines()[-1])
            assert result["status"] == "completed", result
            assert result["revision"] == storage_scope(root, identity).publication_id
            assert "synchronize wiki" in result["unfinished"]
        assert len(service.requests) == 4
