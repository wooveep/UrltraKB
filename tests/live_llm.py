"""One real DeepSeek client per test run, with exact-request coalescing.

There are no canned answers or model-output factories here. Cached values are
unchanged responses received from DeepSeek during this same run. Callers keep
their complete prompts; fixtures must reduce data before request preparation.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import portalocker
import pytest
from dotenv import dotenv_values
from live_errors import ProviderHTTPError
from live_network import admitted_transport, install_network_guard
from live_request import MAX_CONTEXT_BYTES as MAX_CONTEXT_BYTES
from live_request import provider_request, request_changes, sdk_request
from llm_test_budget import TestRunBudget

ROOT = Path(__file__).resolve().parents[1]
_SDK_FUNCTIONS = None
_FAILURE_PATH = None
_FAILURE_SECRETS = ()


def _register_failure_secrets(values):
    global _FAILURE_SECRETS
    _FAILURE_SECRETS = tuple(
        set(_FAILURE_SECRETS)
        | {
            value
            for key, value in values.items()
            if value and any(name in key.upper() for name in ("API_KEY", "SECRET", "PASSWORD"))
        }
    )


def peak_period(instant: datetime) -> bool:
    local = instant.astimezone(ZoneInfo("Asia/Shanghai"))
    # Reservation always uses peak prices. Tests outside the workday windows
    # are certainly idle; public-holiday runs in these windows report the peak
    # upper bound unless a verified holiday calendar is supplied separately.
    return local.weekday() < 5 and (9 <= local.hour < 12 or 14 <= local.hour < 18)


class LiveLLM:
    def __init__(self, budget: TestRunBudget, *, env_path: Path = ROOT / ".env", profile="bounded"):
        import litellm

        global _SDK_FUNCTIONS
        if _SDK_FUNCTIONS is None:
            _SDK_FUNCTIONS = (litellm.completion, litellm.acompletion)
        self._sdk_completion, self._sdk_acompletion = _SDK_FUNCTIONS
        values = dotenv_values(env_path)
        _register_failure_secrets(values)
        self._key = values.get("LLM_API_KEY") or values.get("OPENAI_API_KEY")
        base = (values.get("OPENAI_API_BASE") or "https://api.deepseek.com").rstrip("/")
        if not self._key:
            raise ValueError(f"Real LLM tests require LLM_API_KEY in {env_path}")
        parsed = urlparse(base)
        if parsed.scheme != "https" or parsed.hostname != "api.deepseek.com":
            raise ValueError("Real test requests must use the official DeepSeek HTTPS endpoint")
        self._url = base.removesuffix("/v1") + "/chat/completions"
        self._base = base
        self.budget = budget
        self.profile = profile
        self.requests = []
        self.calls = []
        self.adjustments = []
        self.receipts = []
        self.reused = 0
        self._cache_path = budget.path.with_suffix(".responses.json")
        self._cache = json.loads(self._cache_path.read_text()) if self._cache_path.exists() else {}
        self._lock = threading.RLock()
        self._http = httpx.Client(timeout=90, trust_env=False)
        from live_proxy import DeepSeekProxy

        self._proxy = DeepSeekProxy(self)

    def close(self):
        self._proxy.close()
        self._http.close()

    def _request_lock(self, fingerprint):
        return portalocker.Lock(
            self._cache_path.parent / f".{self._cache_path.stem}.request-{fingerprint}.lock",
            timeout=120,
        )

    def _cached_response(self, fingerprint):
        with portalocker.Lock(str(self._cache_path) + ".lock", timeout=20):
            current = json.loads(self._cache_path.read_text()) if self._cache_path.exists() else {}
            return copy.deepcopy(current.get(fingerprint))

    def _save_response(self, fingerprint, value):
        with portalocker.Lock(str(self._cache_path) + ".lock", timeout=20):
            current = json.loads(self._cache_path.read_text()) if self._cache_path.exists() else {}
            current[fingerprint] = copy.deepcopy(value)
            self._cache = current
            self._write_responses(current)

    def _write_responses(self, current):
        descriptor, temporary = tempfile.mkstemp(
            dir=self._cache_path.parent, prefix=".real-responses-"
        )
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(current, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._cache_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @property
    def credentials(self):
        from ultrakb.config import LlmCredentialBundle

        return LlmCredentialBundle(api_key=self._key, base_url=self._proxy.url)

    def _payload(self, kwargs):
        return provider_request(kwargs, profile=self.profile)

    def _check_frozen_credentials(self, headers):
        from ultrakb.llm_usage_transport import _CALL

        if call := _CALL.get():
            call.check_credentials(headers)

    @staticmethod
    def _public_request(kwargs):
        result = copy.deepcopy(
            {key: value for key, value in kwargs.items() if key not in {"api_key", "client"}}
        )
        if "extra_headers" in result:
            result["extra_headers"] = {
                name: "<redacted>"
                if name.lower() in {"authorization", "api-key", "x-api-key"}
                else value
                for name, value in result["extra_headers"].items()
            }
        return result

    def request(self, **kwargs) -> dict:
        if kwargs.get("stream"):
            raise ValueError("Use acompletion for real streaming tests")
        payload, output_limit = self._payload(kwargs)
        self.adjustments.append({"hop": "provider", "fields": request_changes(kwargs, payload)})
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(serialized.encode()).hexdigest()
        headers = {
            **(kwargs.get("extra_headers") or {}),
            "Authorization": f"Bearer {self._key}",
        }
        self._check_frozen_credentials(headers)
        with self._request_lock(fingerprint), self._lock:
            self.requests.append(self._public_request(kwargs))
            if cached := self._cached_response(fingerprint):
                self.reused += 1
                return cached
            # UTF-8 bytes are a conservative BPE-token bound; protocol framing
            # gets an additional reserve. No tokenizer approximation can silently
            # admit an expensive request based on an underestimated prompt.
            identifier = self.budget.reserve(
                input_tokens=len(serialized.encode("utf-8")) + 1024,
                output_tokens=output_limit,
                fingerprint=fingerprint,
            )
            period = peak_period(datetime.now(ZoneInfo("Asia/Shanghai")))
            try:
                with admitted_transport(self._http, self.budget, identifier, fingerprint):
                    response = self._http.post(
                        self._url,
                        headers=headers,
                        json=payload,
                        timeout=kwargs.get("timeout", 90),
                    )
                if not response.is_success:
                    raise ProviderHTTPError(response)
                result = response.json()
                usage = result.get("usage")
                if not isinstance(usage, dict):
                    raise RuntimeError("Real DeepSeek response omitted its billing receipt")
                self.budget.settle(identifier, usage, peak=period)
                self.receipts.append({"fingerprint": fingerprint, "usage": copy.deepcopy(usage)})
                self._save_response(fingerprint, result)
                return result
            except Exception as error:
                self.budget.uncertain(identifier, type(error).__name__)
                raise

    def completion(self, **kwargs):
        self.calls.append(self._public_request(kwargs))
        return self._sdk_completion(**self._sdk_kwargs(kwargs))

    async def acompletion(self, **kwargs):
        self.calls.append(self._public_request(kwargs))
        result = await self._sdk_acompletion(**self._sdk_kwargs(kwargs))
        return result

    def _sdk_kwargs(self, kwargs):
        # The production SDK still performs message/tool/stream serialization.
        # The loopback gateway owns request coalescing and actual cost admission.
        values = sdk_request(
            kwargs, api_key=self._key, api_base=self._proxy.url, profile=self.profile
        )
        self.adjustments.append({"hop": "sdk", "fields": request_changes(kwargs, values)})
        return values

    async def remote_acompletion(self, **kwargs):
        if kwargs.get("stream"):
            from live_stream import DeepSeekStream, ReusedStream

            payload, output_limit = self._payload(kwargs)
            self.adjustments.append({"hop": "provider", "fields": request_changes(kwargs, payload)})
            serialized = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            fingerprint = hashlib.sha256(serialized.encode()).hexdigest()
            headers = {
                **(kwargs.get("extra_headers") or {}),
                "Authorization": f"Bearer {self._key}",
            }
            self._check_frozen_credentials(headers)
            lease = self._request_lock(fingerprint)
            await asyncio.to_thread(lease.__enter__)
            try:
                self.requests.append(self._public_request(kwargs))
                if cached := self._cached_response(fingerprint):
                    self.reused += 1
                    lease.__exit__(None, None, None)
                    return ReusedStream(cached["stream_chunks"])
                identifier = self.budget.reserve(
                    input_tokens=len(serialized.encode()) + 1024,
                    output_tokens=output_limit,
                    fingerprint=fingerprint,
                )
                stream = DeepSeekStream(
                    self,
                    payload,
                    fingerprint,
                    identifier,
                    peak_period(datetime.now(ZoneInfo("Asia/Shanghai"))),
                    headers,
                    kwargs.get("timeout", 90),
                    lease=lease,
                )
                return await stream.start()
            except BaseException:
                lease.__exit__(None, None, None)
                raise
        from litellm import ModelResponse

        return ModelResponse(**await asyncio.to_thread(self.request, **kwargs))

    def install(self, monkeypatch):
        """Bind the actual model port to this run's real, accounted client."""
        # The application's frozen credential check runs before transport I/O.
        # It must capture the same real credential as this client, rather than
        # silently falling back to an unrelated process key.
        monkeypatch.setenv("LLM_API_KEY", self._key)
        monkeypatch.setenv("OPENAI_API_BASE", self._proxy.url)
        monkeypatch.setattr("litellm.completion", self.completion)
        monkeypatch.setattr("litellm.acompletion", self.acompletion)


@pytest.fixture(scope="session")
def live_llm(request):
    report = request.config.getoption("llm_cost_report")
    if report is None:
        raise pytest.UsageError("Real LLM tests require --llm-cost-report=PATH")
    client = LiveLLM(TestRunBudget(Path(report)))
    yield client
    client.close()


@pytest.fixture(scope="session")
def production_llm(live_llm):
    """Same cost ledger, preserving production thinking and output parameters."""
    client = LiveLLM(live_llm.budget, profile="production")
    yield client
    client.close()


def pytest_addoption(parser):
    parser.addoption(
        "--llm-cost-report", help="Persistent ledger for this full test run (<= 2 CNY)"
    )


def pytest_configure(config):
    import litellm
    from agents import set_tracing_disabled

    global _SDK_FUNCTIONS, _FAILURE_PATH, _FAILURE_SECRETS
    _SDK_FUNCTIONS = (litellm.completion, litellm.acompletion)
    set_tracing_disabled(True)
    os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
    install_network_guard()
    if config.getoption("llm_cost_report") is None:
        stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
        config.option.llm_cost_report = str(
            ROOT / ".scratch/llm-test-runs" / f"{stamp}-{uuid4().hex[:8]}" / "cost.json"
        )
    config.option.llm_cost_report = str(Path(config.option.llm_cost_report).resolve())
    _FAILURE_PATH = Path(config.option.llm_cost_report).with_suffix(".failures.jsonl")
    _register_failure_secrets(os.environ)


def _redact_failure(text):
    for secret in _FAILURE_SECRETS:
        text = text.replace(secret, "<redacted>")
    return text


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.longrepr is not None:
        original = str(report.longrepr)
        redacted = _redact_failure(original)
        if redacted != original:
            report.longrepr = redacted
    report.sections = [(name, _redact_failure(value)) for name, value in report.sections]
    return report


def pytest_runtest_logreport(report):
    """Persist failures immediately, so long live runs can be diagnosed safely."""
    if not report.failed or _FAILURE_PATH is None:
        return
    detail = _redact_failure(str(report.longrepr))
    _FAILURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {"nodeid": report.nodeid, "phase": report.when, "detail": detail[:16000]}
    with _FAILURE_PATH.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    _FAILURE_PATH.chmod(0o600)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    report = config.getoption("llm_cost_report")
    if report and Path(report).exists():
        summary = TestRunBudget(Path(report)).report()
        terminalreporter.write_line(
            f"Real deepseek-flash: {summary['provider_requests']} provider sends; "
            f"{summary['requests']} admissions; "
            f"usage-based cost CNY {summary['actual_cny']}; "
            f"maximum exposure CNY {summary['exposure_cny']} / {summary['limit_cny']}"
        )
