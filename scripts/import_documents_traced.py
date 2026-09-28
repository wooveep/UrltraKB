"""Import explicit PDFs through the normal pipeline with raw provider audit files.

Run with the project's Python: python -m scripts.import_documents_traced ...
The loopback proxy is private to this run. Credentials stay in the KB's .env;
request/response bodies and per-attempt usage are recorded outside rollback paths.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

from openkb.locks import atomic_write_json, kb_ingest_lock
from scripts.llm_trace_proxy import AuditProxy, utc_now


def worker(kb: Path, source: Path, result_path: Path) -> int:
    from openkb.application.documents import import_document
    from openkb.cli import _setup_llm_key
    from openkb.config import resolve_credential_bundle

    _setup_llm_key(kb)
    try:
        result = import_document(kb, source, bundle=resolve_credential_bundle(kb), report=print)
        data = asdict(result)
        atomic_write_json(result_path, data)
        return 0 if result.status in {"added", "skipped"} and not result.unfinished else 1
    except Exception as exc:
        # The parent redacts the detailed process log. Keep the structured result
        # to the exception type so a provider's message cannot persist credentials.
        atomic_write_json(result_path, {"status": "failed", "error_type": type(exc).__name__})
        print(f"Import failed: {type(exc).__name__}", flush=True)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb", type=Path, required=True)
    parser.add_argument("--model", default="deepseek/deepseek-flash")
    parser.add_argument("--language", default="zh-cn")
    parser.add_argument("--timeout", type=float, default=1200)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--upstream", default="https://api.deepseek.com")
    parser.add_argument("--worker-result", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("files", type=Path, nargs="+")
    args = parser.parse_args()
    kb = args.kb.resolve()
    files = [p.resolve(strict=True) for p in args.files]
    if args.worker_result:
        return worker(kb, files[0], args.worker_result)
    if not args.model.startswith("deepseek/") or not args.upstream.startswith("https://"):
        parser.error("This import recorder requires a DeepSeek model and HTTPS provider endpoint")
    if args.timeout <= 0 or args.concurrency <= 0:
        parser.error("Timeout and concurrency must be positive")
    os.umask(0o077)
    values = dotenv_values(kb / ".env")
    api_key = values.get("LLM_API_KEY")
    if not api_key:
        parser.error("The knowledge base .env must contain LLM_API_KEY")
    if values.get("OPENAI_API_BASE"):
        parser.error(
            "Use --upstream for the audit destination; remove the KB-local base override first"
        )
    from openkb.application.knowledge_bases import initialize_kb
    from openkb.config import load_config, save_config

    if not (kb / ".openkb/config.yaml").exists():
        initialize_kb(kb, model=args.model, language=args.language, seed_environment=False)
    with kb_ingest_lock(kb / ".openkb"):
        config = load_config(kb / ".openkb/config.yaml")
        config.update(
            model=args.model,
            language=args.language,
            timeout=args.timeout,
            concurrency=args.concurrency,
        )
        save_config(kb / ".openkb/config.yaml", config)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    directory = kb / ".openkb/traces" / run_id
    directory.mkdir(parents=True, mode=0o700)
    manifest = {
        "run_id": run_id,
        "started_at": utc_now(),
        "kb": str(kb),
        "model": args.model,
        "language": args.language,
        "timeout_seconds": args.timeout,
        "concurrency": args.concurrency,
        "upstream": args.upstream,
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "documents": [],
    }
    import pymupdf

    for source in files:
        with pymupdf.open(source) as pdf:
            manifest["documents"].append(
                {
                    "source": str(source),
                    "pages": len(pdf),
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "status": "pending",
                }
            )
    atomic_write_json(directory / "manifest.json", manifest)
    print(
        json.dumps(
            {"trace_directory": str(directory), "documents": len(files)}, ensure_ascii=False
        ),
        flush=True,
    )
    failed = False
    with AuditProxy(
        directory / "requests", upstream=args.upstream, timeout=args.timeout, api_key=api_key
    ) as audit:
        for i, (source, item) in enumerate(zip(files, manifest["documents"]), 1):
            audit.document = source.name
            item.update(status="running", started_at=utc_now())
            atomic_write_json(directory / "manifest.json", manifest)
            print(f"[{i}/{len(files)}] {source.name}: {item['pages']} pages", flush=True)
            result_path = directory / f"{i:02d}-result.json"
            environment = dict(os.environ)
            environment.update(
                OPENAI_API_BASE=audit.base_url,
                DEEPSEEK_API_BASE=audit.base_url,
                LLM_API_KEY=api_key,
                DEEPSEEK_API_KEY=api_key,
                OPENKB_LLM_AUDIT="1",
                PYTHONUNBUFFERED="1",
                LITELLM_LOCAL_MODEL_COST_MAP="True",
            )
            command = [
                sys.executable,
                "-m",
                "scripts.import_documents_traced",
                "--kb",
                str(kb),
                "--worker-result",
                str(result_path),
                str(source),
            ]
            with (directory / f"{i:02d}-console.log").open("x", encoding="utf-8") as log:
                process = subprocess.Popen(
                    command,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                assert process.stdout is not None
                for line in process.stdout:
                    log.write(audit.redact(line))
                    log.flush()
                code = process.wait()
            result = json.loads(result_path.read_text()) if result_path.exists() else {}
            # Scrub any strings a downstream error result may contain before retaining it.
            if result_path.exists():
                atomic_write_json(result_path, audit.redact(result))
            item.update(
                status=result.get("status", "failed"),
                exit_code=code,
                finished_at=utc_now(),
                result=result_path.name,
            )
            failed |= code != 0
            atomic_write_json(directory / "manifest.json", manifest)
            print(f"[{i}/{len(files)}] {source.name}: {item['status']} (exit {code})", flush=True)
    manifest.update(finished_at=utc_now(), status="failed" if failed else "completed")
    atomic_write_json(directory / "manifest.json", manifest)
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "trace_directory": str(directory),
                "requests": audit.summary()["requests"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
