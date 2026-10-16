"""Connection readiness, bounded real probes and private cross-KB verification records.

Reading readiness never calls a model. Knowledge operations retain their existing
execution rules; the desktop gate uses this service without imposing an API/CLI wizard.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ultrakb import config
from ultrakb.application.connection_failures import connection_failure, failure_for_code
from ultrakb.config import LlmCredentialBundle
from ultrakb.config_state import ConfigSnapshot, capture_config
from ultrakb.locks import atomic_write_json, atomic_write_text, file_write_lock, kb_ingest_lock

CAPABILITIES = ("文本响应", "一次工具调用及结果回读")
_RUNNING: set[str] = set()
_GUARD = threading.Lock()


@dataclass(frozen=True)
class Connection:
    model: str
    credentials: LlmCredentialBundle = field(repr=False)
    generation: dict = field(repr=False)
    sources: dict[str, str]
    identity: str = field(repr=False)
    missing: tuple[str, ...] = ()
    state_dir: Path = field(default_factory=lambda: config.GLOBAL_CONFIG_DIR, repr=False)
    legacy_identity: str | None = field(default=None, repr=False)
    capacity_reason: str | None = None


@dataclass(frozen=True)
class ConnectionState:
    state: str
    model: str
    message: str
    sources: dict[str, str] = field(default_factory=dict)
    tested_at: str | None = None
    capabilities: tuple[str, ...] = ()
    code: str | None = None
    capacity_ready: bool | None = None


class ConnectionCapabilityError(ValueError):
    pass


class _VerificationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    attempt: str = Field(pattern=r"^[a-f0-9]{32}$")
    state: Literal["testing", "passed", "failed", "unverified"]
    capabilities: list[str] = Field(default_factory=list)
    tested_at: datetime | None = None
    code: str | None = Field(default=None, max_length=40)
    message: str | None = Field(default=None, max_length=200)


def _connection_lock(root: Path):
    return (
        config._with_global_config_lock()
        if root == config.GLOBAL_CONFIG_DIR
        else file_write_lock(root / "global.lock")
    )


def _identity(material: dict, root: Path) -> str:
    """A private, keyed identity; neither secret nor derived material enters feedback."""
    key_path = root / "connection-identity.key"
    with _connection_lock(root):
        if not key_path.exists():
            key_path.touch(mode=0o600)
            atomic_write_text(key_path, secrets.token_hex(32))
        key = bytes.fromhex(key_path.read_text().strip())
        if len(key) != 32:
            raise ValueError("Connection identity state needs recovery")
        return hmac.new(
            key, json.dumps(material, sort_keys=True).encode(), hashlib.sha256
        ).hexdigest()


def connection_from_snapshot(snapshot: ConfigSnapshot) -> Connection:
    """Use the immutable configuration actually captured after the execution lease."""
    return _from_values(snapshot.values())


def _from_values(values: dict) -> Connection:
    effective = values["effective"]
    model = effective["model"]
    credentials = dict(values["credentials"])
    origins = dict(values.get("credential_sources", {}))
    environment = values.get("environment", {})
    provider = model.split("/", 1)[0] if "/" in model else "openai"
    if credentials["api_key"] is None:
        credentials["api_key"] = environment.get(provider.upper() + "_API_KEY") or environment.get(
            "OPENAI_API_KEY"
        )
        if credentials["api_key"]:
            origins["api_key"] = "environment"
    bundle = LlmCredentialBundle(**credentials)
    from ultrakb.llm_execution import _SDK_AUTH_PROVIDERS

    missing = []
    if not model.strip():
        missing.append("填写模型名称")
    if not bundle.api_key and provider not in _SDK_AUTH_PROVIDERS:
        missing.append("填写此服务的 API Key（也可由启动环境或全局设置提供）")
    if bundle.base_url:
        parsed = urlsplit(bundle.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            missing.append("填写有效的 http/https 服务地址")
    capacity_reason = None
    if not missing:
        from ultrakb.execution_capacity import capacity_policy
        from ultrakb.model_tokenizer import TokenMeasure

        try:
            TokenMeasure(
                capacity_policy(
                    effective,
                    custom_endpoint=bool(bundle.base_url),
                    tokenizer_snapshot=values.get("tokenizer_asset"),
                    base_url=bundle.base_url,
                )
            )
        except (ValueError, OSError):
            capacity_reason = "模型容量未确认；AI 处理开始前需要配置当前服务的容量。"
    sources = {"model": values["sources"].get("model", "default"), **origins}
    generation = effective.get("role_generation") or {}
    # SDK/workload credentials are part of the actual connection as well. The
    # captured environment is included privately, so rotations cannot reuse a pass.
    auth = {
        name: value
        for name, value in environment.items()
        if any(
            part in name.upper() for part in ("API_KEY", "TOKEN", "SECRET", "CREDENTIAL", "AWS_")
        )
    }
    state_dir = Path(values.get("global_config_dir") or config.GLOBAL_CONFIG_DIR)
    material = {
        "version": 1,
        "model": model,
        "credentials": credentials,
        "generation": generation,
        "sdk_auth": auth if provider in _SDK_AUTH_PROVIDERS else {},
    }
    # Provenance explains a setting; it does not change the connection being
    # tested. Identical explicit and inherited values must reuse the same proof.
    identity = _identity(material, state_dir)
    legacy_identity = _identity({**material, "sources": sources}, state_dir)
    return Connection(
        model,
        bundle,
        generation,
        sources,
        identity,
        tuple(missing),
        state_dir,
        legacy_identity,
        capacity_reason=capacity_reason,
    )


def capture_connection(kb_dir: Path | None = None) -> Connection:
    with kb_ingest_lock(kb_dir / ".ultrakb") if kb_dir else nullcontext():
        if kb_dir:
            return connection_from_snapshot(capture_config(kb_dir))
        from dotenv import dotenv_values

        with config._with_global_config_lock():
            global_values = config._load_global_config_unlocked()
            effective = dict(config.DEFAULT_CONFIG)
            sources = {key: "default" for key in config.GLOBAL_SCALAR_KEYS}
            for key in config.GLOBAL_SCALAR_KEYS:
                if global_values.get(key) is not None:
                    effective[key], sources[key] = global_values[key], "global"
            bundle, origins = config.resolve_connection_credentials(model=effective["model"])
            environment = dict(os.environ)
            for key, value in dotenv_values(config.GLOBAL_CONFIG_DIR / ".env").items():
                if value and not environment.get(key):
                    environment[key] = value
            return _from_values(
                {
                    "effective": effective,
                    "credentials": asdict(bundle),
                    "credential_sources": origins,
                    "sources": sources,
                    "environment": environment,
                }
            )


def _record_path(connection: Connection) -> Path:
    return connection.state_dir / "connections" / f"{connection.identity}.json"


def _read_record(connection: Connection) -> dict:
    path = _record_path(connection)
    legacy = connection.state_dir / "connections" / f"{connection.legacy_identity}.json"
    source = path if path.is_file() else legacy
    if not source.is_file() and connection.state_dir == config.GLOBAL_CONFIG_DIR:
        # Existing installations may have tested the global connection before
        # adding identical KB overrides. Reuse that old proof during migration.
        global_connection = capture_connection()
        if global_connection.identity == connection.identity:
            source = (
                connection.state_dir / "connections" / f"{global_connection.legacy_identity}.json"
            )
    if not source.is_file():
        return {}
    try:
        record = _VerificationRecord.model_validate_json(source.read_text()).model_dump(
            mode="json", exclude_none=True
        )
        if source != path and record["state"] == "passed":
            atomic_write_json(path, record)
        return record
    except (ValueError, OSError):
        return {}


def connection_state(connection: Connection) -> ConnectionState:
    if connection.missing:
        return ConnectionState(
            "missing", connection.model, "；".join(connection.missing), connection.sources
        )
    with _connection_lock(connection.state_dir):
        record = _read_record(connection)
    state = record.get("state", "unverified")
    if (
        state == "passed"
        and record.get("capabilities") == list(CAPABILITIES)
        and record.get("tested_at")
    ):
        return ConnectionState(
            state,
            connection.model,
            "连接测试通过。" + (connection.capacity_reason or ""),
            connection.sources,
            record.get("tested_at"),
            CAPABILITIES,
            capacity_ready=connection.capacity_reason is None,
        )
    if state == "passed":
        state = "unverified"
    if state == "testing":
        with _GUARD:
            active = record.get("attempt") in _RUNNING
        if active:
            return ConnectionState(
                state, connection.model, "正在测试文本与工具调用…", connection.sources
            )
        state = "unverified"
    return ConnectionState(
        state,
        connection.model,
        failure_for_code(record.get("code")).message
        if state in {"failed", "unverified"} and record.get("code")
        else "已保存，尚未验证。请主动测试连接。",
        connection.sources,
        code=record.get("code"),
        capacity_ready=connection.capacity_reason is None,
    )


def read_connection_state(kb_dir: Path | None = None) -> ConnectionState:
    return connection_state(capture_connection(kb_dir))


class ConnectionRequired(ValueError):
    """A desktop operation needs setup for its actual leased configuration."""


def require_verified_connection(snapshot: ConfigSnapshot) -> None:
    state = connection_state(connection_from_snapshot(snapshot))
    if state.state != "passed":
        raise ConnectionRequired("实际连接尚未验证或已改变；请完成设置并重新提交原操作。")


async def _probe(connection: Connection, cancelled: Callable[[], bool], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    from agents import Agent, ModelSettings, RunConfig, Runner, function_tool

    from ultrakb.agent.managed_model import ManagedAgentModel
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings

    called = []

    @function_tool
    def connection_check(value: str) -> str:
        """Echo the synthetic connection-check marker; never reads any user data."""
        if value != "ultrakb-connection-check":
            raise ConnectionCapabilityError("Unexpected synthetic marker")
        called.append(value)
        return "OK"

    generation = dict(connection.generation)
    answer = dict(generation.get("answer") or {})
    answer["max_tokens"] = min(answer.get("max_tokens", 512), 512)
    generation["answer"] = answer
    policy = ModelCallPolicy(
        max_calls=2, concurrency=1, retries=0, cancelled=cancelled, deadline=deadline
    )
    policy.check()
    executor = CompletionExecutor(
        RoleBindings(
            connection.model,
            replace(
                connection.credentials,
                timeout=min(connection.credentials.timeout or timeout, timeout),
            ),
            generation=generation,
        ),
        policy,
    )
    model = ManagedAgentModel(executor)
    agent = Agent(
        name="connection-check",
        instructions="Call connection_check exactly once with value 'ultrakb-connection-check'. "
        "After receiving its result, reply with OK. This is synthetic connection testing.",
        model=model,
        tools=[connection_check],
        model_settings=ModelSettings(
            max_tokens=512, parallel_tool_calls=False, retry=model.retry_settings()
        ),
    )
    with executor.activate():
        result = await Runner.run(
            agent,
            "Test this synthetic connection.",
            max_turns=2,
            run_config=RunConfig(tracing_disabled=True),
        )
    executor.policy.check()
    if called != ["ultrakb-connection-check"] or not str(result.final_output or "").strip():
        raise ConnectionCapabilityError("Text and tool round trip were not completed")


def verify_model_connection(
    kb_dir: Path | None = None,
    *,
    cancelled: Callable[[], bool] = lambda: False,
    timeout: float = 20,
) -> ConnectionState:
    """Capture once, test with bounded synthetic input, then compare current settings.

    No configuration/global lease is held across the network. A newer attempt or
    changed connection cannot be marked passed by this attempt's late result.
    """
    if not 0 < timeout <= 60:
        raise ValueError("Connection test timeout must be between 0 and 60 seconds")
    connection = capture_connection(kb_dir)
    if connection.missing:
        return connection_state(connection)
    attempt = uuid4().hex
    record = {"version": 1, "state": "testing", "attempt": attempt}
    with config._with_global_config_lock():
        previous = _read_record(connection)
        with _GUARD:
            if previous.get("state") == "testing" and previous.get("attempt") in _RUNNING:
                return ConnectionState(
                    "testing",
                    connection.model,
                    "当前连接已有测试正在进行，请稍后查看结果。",
                    connection.sources,
                )
            _RUNNING.add(attempt)
        try:
            atomic_write_json(_record_path(connection), record)
        except BaseException:
            with _GUARD:
                _RUNNING.discard(attempt)
            raise
    try:
        try:
            from ultrakb.model_log_secrets import redact_model_logs

            with redact_model_logs(connection.credentials):
                asyncio.run(_probe(connection, cancelled, timeout))
            record.update(
                state="passed",
                capabilities=list(CAPABILITIES),
                tested_at=datetime.now(timezone.utc).isoformat(),
            )
        except Exception as error:
            failure = connection_failure(error)
            record.update(
                state="unverified" if failure.code == "cancelled" else "failed",
                message=failure.message,
                code=failure.code,
            )
        with config._with_global_config_lock():
            if _read_record(connection).get("attempt") == attempt:
                if cancelled():
                    record.update(
                        state="unverified",
                        code="cancelled",
                        message="测试已取消；已保存的设置保留。",
                    )
                atomic_write_json(_record_path(connection), record)
        current = capture_connection(kb_dir)
        if current.identity != connection.identity:
            return ConnectionState(
                "unverified",
                current.model,
                "测试期间连接发生变化，请测试当前配置。",
                current.sources,
                code="changed",
            )
        return connection_state(current)
    finally:
        with _GUARD:
            _RUNNING.discard(attempt)
