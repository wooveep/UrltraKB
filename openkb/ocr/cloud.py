"""Explicit single-page jobs, persisted before POST, resumed by known job identity."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import pymupdf
import requests

from openkb.compilation_report import report_auxiliary_warning
from openkb.config_state import execution_environment
from openkb.evidence import BlockDraft, ParseStore
from openkb.locks import atomic_write_json
from openkb.ocr.config import CloudSettings
from openkb.processing import processing_checkpoint
from openkb.sources import SourceStore, SourceVersion, content_id, read_object


class CloudIncomplete(Exception):
    pass


class CloudJobs:
    def __init__(
        self, store: SourceStore, source: SourceVersion, config: CloudSettings, *, retries=None
    ):
        self.store, self.source, self.config = store, source, config
        self.started = time.monotonic()
        self.requests = self.pages = self.downloaded = 0
        self.session = requests.Session()
        self.session.trust_env = False
        self.token = execution_environment(store.kb_dir).get(config.credential_env)
        self.record: dict = {}
        self.path: Path | None = None
        self.remote_may_continue = False
        self.retries = retries or {}

    def close(self):
        self.session.close()
        if self.remote_may_continue:
            report_auxiliary_warning("cloud_job_may_continue_after_local_wait")

    def _checkpoint(self):
        processing_checkpoint("ocr")
        if time.monotonic() - self.started >= self.config.limits.seconds:
            raise CloudIncomplete("ocr_time_budget_exhausted")

    def _save(self, path: Path):
        # Remote execution facts cannot be rolled back when local cancellation
        # arrives. The caller retains the KB lease; publish one atomic receipt.
        atomic_write_json(self.store.owned_path(path), self.record)

    def _request(self, method: str, url: str, *, authenticated=False, **kwargs):
        self._checkpoint()
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise CloudIncomplete("cloud_invalid_resource_url")
        if self.requests >= self.config.limits.max_requests:
            raise CloudIncomplete("ocr_request_budget_exhausted")
        self.requests += 1
        self.record["requests"] = self.record.get("requests", 0) + 1
        if method == "POST":
            self.record["state"] = "submitting"
            self.record["submissions"] = self.record.get("submissions", 0) + 1
        if self.path is None:
            raise CloudIncomplete("cloud_checkpoint_missing")
        self._save(self.path)
        timeout = min(
            self.config.limits.request_seconds,
            self.config.limits.seconds - (time.monotonic() - self.started),
        )
        headers = {"Authorization": f"Bearer {self.token}"} if authenticated else {}
        return self.session.request(
            method,
            url,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
            stream=True,
            **kwargs,
        )

    def _data(self, response):
        try:
            body = json.loads(self._body(response))
        except (ValueError, TypeError):
            raise CloudIncomplete("cloud_invalid_response") from None
        if not isinstance(body, dict):
            raise CloudIncomplete("cloud_invalid_response")
        code = body.get("code", 0)
        if type(code) is not int:
            raise CloudIncomplete("cloud_invalid_response")
        self.record.update(http_status=response.status_code, service_code=code)
        if self.path is not None:
            self._save(self.path)
        reasons = {
            12001: "cloud_daily_quota_exhausted",
            12002: "cloud_rate_limited",
            11001: "cloud_job_not_found",
            11002: "cloud_job_expired",
            11003: "cloud_job_failed",
        }
        if code:
            raise CloudIncomplete(reasons.get(code, f"cloud_service_error_{code}"))
        if response.status_code >= 400:
            raise CloudIncomplete(f"cloud_http_{response.status_code}")
        data = body.get("data")
        if not isinstance(data, dict):
            raise CloudIncomplete("cloud_invalid_response")
        return data

    def _download(self, url: str) -> bytes:
        with self._request("GET", url) as response:
            if response.status_code != 200:
                raise CloudIncomplete("cloud_result_unavailable")
            return self._body(response)

    def _body(self, response) -> bytes:
        chunks = []
        try:
            for block in response.iter_content(65536):
                self._checkpoint()
                self.downloaded += len(block)
                self.record["download_bytes"] = self.record.get("download_bytes", 0) + len(block)
                if self.downloaded > self.config.limits.max_download_bytes:
                    raise CloudIncomplete("ocr_download_budget_exhausted")
                chunks.append(block)
        finally:
            if self.path is not None:
                self._save(self.path)
        return b"".join(chunks)

    def page(self, document: pymupdf.Document, page: int) -> tuple[list[BlockDraft], str | None]:
        """The only output page belongs to this physically extracted original page."""
        self.path, self.record = None, {}
        try:
            return self._page(document, page)
        except CloudIncomplete as exc:
            if self.path is not None:
                self.record["reason"] = str(exc)
                self._save(self.path)
            return [], str(exc)
        except requests.RequestException:
            if self.path is not None:
                self.record["reason"] = "cloud_transport_unavailable"
                self._save(self.path)
            return [], "cloud_transport_unavailable"
        finally:
            if self.record.get("state") in {"submitting", "submission_unknown", "submitted"}:
                self.remote_may_continue = True

    def _page(self, document, page):
        self._checkpoint()
        with pymupdf.open() as sliced:
            sliced.insert_pdf(document, from_page=page - 1, to_page=page - 1)
            content = sliced.tobytes(garbage=4, deflate=True, no_new_id=True)
        if len(content) > self.config.limits.max_page_bytes:
            raise CloudIncomplete("ocr_single_page_input_too_large")
        with pymupdf.open(stream=content, filetype="pdf") as sliced:
            if sliced.page_count != 1 or sliced[0].rect != document[page - 1].rect:
                raise CloudIncomplete("cloud_slice_mapping_invalid")
        digest = self.store.put_bytes(content)
        profile = {"ocr": self.config.profile(), "physical_page": page, "slice": digest}
        if page in self.retries:
            profile["reprocessing"] = self.retries[page]
        intent = {"source": self.source.id, "page": page, "slice": digest, "profile": profile}
        identity = content_id(intent)
        path = self.store.owned_path(self.store.root / "cloud-jobs" / f"{identity}.json")
        self.path = path
        self.record = (
            read_object(path)
            if path.exists()
            else {
                "identity": identity,
                "input": intent,
                "state": "planned",
                "job_id": None,
                "requests": 0,
            }
        )
        if self.record.get("identity") != identity or self.record.get("input") != intent:
            raise CloudIncomplete("cloud_checkpoint_input_mismatch")
        state = self.record.get("state")
        from openkb.ocr.assembly import assembly_profile

        parsed_profile = {**profile, "assembly": assembly_profile("cloud")}
        if state == "downloaded":
            parsed = ParseStore(self.store.kb_dir).load(self.record["parse_id"])
            if {
                k: v for k, v in parsed.profile.items() if k != "assembly"
            } != profile or parsed.input_key != self.source.input_key:
                raise CloudIncomplete("cloud_checkpoint_input_mismatch")
            if parsed.profile != parsed_profile:
                return self._assemble(page, parsed_profile)
            drafts = [
                BlockDraft(
                    self.store.asset(block.blob).read_text(encoding="utf-8"),
                    block.kind,
                    block.location,
                    block.assets,
                    block.context,
                )
                for block in parsed.blocks
            ]
            for draft in drafts:
                for asset in draft.assets:
                    self.store.asset(asset)
            return drafts, self.record.get("quality_reason")
        if state == "raw_downloaded":
            return self._assemble(page, parsed_profile)
        if state in {"submitting", "submission_unknown"}:
            raise CloudIncomplete("cloud_submission_unknown")
        if state == "rejected":
            raise CloudIncomplete(self.record["reason"])
        if state not in {"planned", "submitted"}:
            raise CloudIncomplete("cloud_checkpoint_invalid")
        if not self.token:
            raise CloudIncomplete("cloud_credentials_missing")
        if state == "planned":
            if self.pages >= self.config.limits.max_pages:
                raise CloudIncomplete("ocr_page_budget_exhausted")
            self.pages += 1
            try:
                with self.store.asset(digest).open("rb") as file:
                    response = self._request(
                        "POST",
                        self.config.endpoint,
                        authenticated=True,
                        data={
                            "model": self.config.model,
                            "optionalPayload": json.dumps(
                                self.config.options.model_dump(by_alias=True)
                            ),
                        },
                        files={"file": ("page.pdf", file, "application/pdf")},
                    )
                with response:
                    data = self._data(response)
                job_id = data.get("jobId")
                if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", job_id):
                    raise CloudIncomplete("cloud_invalid_response")
                self.record.update(job_id=job_id, state="submitted")
                self._save(path)
            except (requests.RequestException, CloudIncomplete):
                if self.record["state"] == "planned":
                    raise  # Admission failed before any POST could leave this process.
                # Only a documented, explicit service rejection proves no job was
                # accepted. Everything else retains the uncertain POST window.
                code = self.record.get("service_code")
                rejected = type(code) is int and (10001 <= code <= 10010 or code in {12001, 12002})
                reason = (
                    (
                        {12001: "cloud_daily_quota_exhausted", 12002: "cloud_rate_limited"}.get(
                            code
                        )
                        or f"cloud_service_error_{code}"
                    )
                    if rejected
                    else "cloud_submission_unknown"
                )
                self.record.update(
                    state="rejected" if rejected else "submission_unknown", reason=reason
                )
                self._save(path)
                raise CloudIncomplete(reason) from None
        while True:
            self._checkpoint()
            with self._request(
                "GET",
                self.config.endpoint.rstrip("/") + "/" + quote(self.record["job_id"], safe=""),
                authenticated=True,
            ) as response:
                data = self._data(response)
            self.record["remote_state"] = data.get("state")
            self._save(path)
            if data.get("state") == "failed":
                raise CloudIncomplete("cloud_job_failed")
            if data.get("state") == "done":
                urls = data.get("resultUrl")
                if not isinstance(urls, dict) or not isinstance(urls.get("jsonUrl"), str):
                    raise CloudIncomplete("cloud_result_unavailable")
                raw = self._download(urls["jsonUrl"])
                self.record["result_blob"] = self.store.put_bytes(raw)
                self.record["state"] = "raw_downloaded"
                self._save(path)
                return self._assemble(page, parsed_profile)
            if data.get("state") not in {"pending", "running"}:
                raise CloudIncomplete("cloud_unknown_job_state")
            delay_until = time.monotonic() + self.config.limits.poll_seconds
            while time.monotonic() < delay_until:
                self._checkpoint()
                time.sleep(min(0.1, delay_until - time.monotonic()))

    def _assemble(self, page: int, profile: dict):
        from openkb.ocr.cloud_result import parse_single_page, verify_image

        if self.path is None or not self.record.get("result_blob"):
            raise CloudIncomplete("cloud_checkpoint_invalid")
        raw = self.store.asset(self.record["result_blob"]).read_bytes()

        def asset(url):
            assets = self.record.setdefault("result_assets", {})
            if url not in assets:
                content = self._download(url)
                verify_image(content)
                assets[url] = self.store.put_bytes(content)
                self._save(self.path)
            return self.store.asset(assets[url]).read_bytes()

        try:
            blocks, reason = parse_single_page(raw, page, self.store, asset)
        except CloudIncomplete as exc:
            if str(exc) in {
                "cloud_result_invalid",
                "cloud_result_page_set_mismatch",
                "cloud_required_asset_missing",
            }:
                # Retain the failed bytes for diagnosis, but fetch this known
                # job's result again on continuation; it is not a checkpoint.
                self.record["state"] = "submitted"
                self._save(self.path)
            raise
        quality = [
            {
                "page": page,
                "status": "needs_review" if reason else "verified",
                "reason": reason or "ocr_layout_checked",
            }
        ]
        parsed = ParseStore(self.store.kb_dir).save(self.source, profile, blocks, quality=quality)
        self.record.update(
            state="downloaded", parse_id=parsed.id, quality_reason=reason, reason=reason
        )
        self._save(self.path)
        return blocks, reason
