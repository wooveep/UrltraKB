"""Verified acquisition, extraction and cancellable subprocesses for optional OCR setup."""

import hashlib
import os
import subprocess
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

from openkb.http_stream import run_http, stream_http
from openkb.processing import processing_checkpoint
from openkb.runtime.process_tree import ProcessTree, resume_suspended_process


def digest(path):
    with Path(path).open("rb") as stream:
        value = hashlib.sha256()
        while chunk := stream.read(1024 * 1024):
            processing_checkpoint()
            value.update(chunk)
        return value.hexdigest()


def verified(path, record):
    return (
        path.is_file()
        and not path.is_symlink()
        and path.stat().st_size == record["bytes"]
        and digest(path) == record["sha256"]
    )


def acquire(record, destination, check, progress, offline=None):
    """Resume verified files; partial HTTP bytes are never a ready model."""
    if verified(destination, record):
        return
    check()
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    if offline is None and record.get("bundled"):
        offline = Path(__file__).parents[1] / "desktop" / record["bundled"]
        if not offline.is_file():
            offline = Path(__file__).parents[2] / record["bundled"]
    if offline is not None:
        if not verified(offline, record):
            raise ValueError("ocr_offline_file_missing_or_changed")
        with offline.open("rb") as src, partial.open("wb") as dst:
            while chunk := src.read(1024 * 1024):
                check()
                dst.write(chunk)
    else:
        # Restart an incomplete file if the server does not honor a range request.
        offset = partial.stat().st_size if partial.exists() else 0
        if offset == record["bytes"] and verified(partial, record):
            partial.replace(destination)
            return
        if offset >= record["bytes"]:
            partial.unlink()
            offset = 0
        first = True
        with partial.open("ab" if offset else "wb") as stream:

            def consume(response, chunk):
                nonlocal first
                if first:
                    resume = (
                        offset > 0
                        and response.status_code == 206
                        and response.headers.get("Content-Range", "").startswith(f"bytes {offset}-")
                    )
                    if not resume:
                        stream.seek(0)
                        stream.truncate()
                    first = False
                stream.write(chunk)
                if stream.tell() > record["bytes"]:
                    raise ValueError("ocr_download_size_mismatch")
                progress(destination.name, stream.tell(), record["bytes"])

            run_http(
                stream_http(
                    "GET",
                    record["url"],
                    seconds=14400,
                    check=check,
                    consume=consume,
                    headers={"Range": f"bytes={offset}-"} if offset else {},
                )
            )
    if not verified(partial, record):
        partial.unlink(missing_ok=True)
        raise ValueError("ocr_download_digest_mismatch")
    partial.replace(destination)


def extract(archive, destination, check, *, prefix=None):
    """Extract a verified archive with a bounded expansion and data-only members."""
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as package:
        members = package.getmembers()
        if sum(m.size for m in members) > 2_000_000_000:
            raise ValueError("ocr_archive_expansion_exceeded")
        for member in members:
            check()
            if prefix:
                parts = Path(member.name).parts
                if len(parts) < 2:
                    continue
                member.name = str(Path(*parts[1:]))
            package.extract(member, destination, filter="data")


def run(command, check, log):
    """Own installation descendants through completion, cancellation and cleanup."""
    with log.open("ab") as output:
        process = subprocess.Popen(
            command,
            stdout=output,
            stderr=output,
            start_new_session=os.name != "nt",
            creationflags=0x4 if os.name == "nt" else 0,
            env={**os.environ, "PIP_NO_INDEX": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        )
        tree = None
        try:
            tree = ProcessTree(
                SimpleNamespace(
                    pid=process.pid,
                    is_alive=lambda: process.poll() is None,
                    terminate=process.terminate,
                    kill=process.kill,
                )
            )
            if os.name == "nt":
                resume_suspended_process(process.pid)
            while process.poll() is None:
                check()
                if log.stat().st_size > 2_000_000:
                    raise ValueError("ocr_install_output_exceeded")
                time.sleep(0.05)
            if process.returncode:
                raise ValueError("ocr_install_process_failed")
        finally:
            if tree is not None:
                if tree.alive():
                    tree.terminate(force=True)
                process.wait(timeout=10)
                deadline = time.monotonic() + 10
                while tree.alive() and time.monotonic() < deadline:
                    time.sleep(0.05)
                if tree.alive():
                    raise RuntimeError("ocr_install_cleanup_unconfirmed")
                tree.close()
            elif process.poll() is None:
                process.kill()
                process.wait(timeout=10)
