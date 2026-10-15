"""Model-service failures with safe, actionable feedback shared by local operations."""

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class ConnectionFailure:
    code: str
    message: str
    stops_batch: bool = False


def failure_for_code(code: str | None) -> ConnectionFailure:
    """Recover feedback from a bounded code, never from persisted/provider text."""
    messages = {
        "cancelled": "测试已取消；已保存的设置保留。",
        "timeout": "服务未在测试时限内响应。检查网络或稍后重试。",
        "authentication": "服务拒绝了凭证。检查此服务的 API Key 及生效来源，再重新测试。",
        "permission": "当前账号无权使用此模型。核对模型权限或换用可用模型。",
        "model": "服务找不到模型或地址。核对模型名称和服务地址后重试。",
        "quota": "服务额度不足。检查服务账号的余额或额度，再手动重试。",
        "rate_limit": "服务暂时限流。稍后手动重试，无需重新填写全部设置。",
        "network": "无法连接服务。检查地址、网络和本地服务是否已启动。",
        "parameters": "服务不接受当前参数。核对模型及工具调用支持，或调整高级参数。",
        "capability": "模型未完成文本与工具调用测试。选择支持工具调用的模型后重试。",
        "service": "连接测试未完成。已保存的设置保留，可检查服务状态后重试。",
    }
    safe_code = code if code in messages else "service"
    return ConnectionFailure(
        safe_code,
        messages[safe_code],
        safe_code in {"authentication", "permission", "model", "quota"},
    )


def _billing_failure(error: BaseException, status: int | None) -> bool:
    if status == 402:
        return True
    # LiteLLM can replace the provider code with "429"; the original SDK error
    # remains in the cause/context chain. Prefer that structured code to text.
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        if getattr(current, "status_code", status) in {None, status}:
            body = getattr(current, "body", None)
            if body is None and type(current).__module__.startswith("litellm."):
                # DeepSeek's SDK adapter keeps the entire provider JSON in
                # OpenAIError.message even when it drops body and response data.
                message = getattr(current, "message", None)
                if isinstance(message, str):
                    try:
                        body = json.loads(message)
                    except ValueError:
                        pass
            detail = body.get("error", body) if isinstance(body, dict) else {}
            codes = [getattr(current, "code", None)]
            if isinstance(detail, dict):
                codes.extend((detail.get("code"), detail.get("type")))
            for code in codes:
                if not isinstance(code, str):
                    continue
                if code in {"insufficient_quota", "billing_hard_limit_reached"}:
                    return True
                if code in {"rate_limit_exceeded", "rate_limit_error", "too_many_requests"}:
                    return False
        pending.extend(
            cause for cause in (current.__cause__, current.__context__) if cause is not None
        )
    return any(
        word in str(error).lower()
        for word in (
            "insufficient_quota",
            "insufficient quota",
            "billing",
            "insufficient balance",
            "balance exhausted",
            "credit balance",
            "余额",
            "额度不足",
            "额度耗尽",
        )
    )


def connection_failure(error: BaseException) -> ConnectionFailure:
    """Classify internally; never copy provider error text or credentials to feedback."""
    category = type(error).__name__
    status = getattr(error, "status_code", None)
    code = "service"
    if category in {"LockCancelled", "CancelledError"}:
        code = "cancelled"
    elif category in {"ModelDeadlineExceeded", "Timeout", "APITimeoutError", "TimeoutError"}:
        code = "timeout"
    elif category == "AuthenticationError" or status == 401:
        code = "authentication"
    elif category == "PermissionDeniedError" or status == 403:
        code = "permission"
    elif category == "NotFoundError" or status == 404:
        code = "model"
    elif category == "RateLimitError" or status in {402, 429}:
        # Error text stays internal: classify quota separately from transient rate limiting.
        code = "quota" if _billing_failure(error, status) else "rate_limit"
    elif category in {"APIConnectionError", "ConnectError", "ConnectionError"}:
        code = "network"
    elif category in {"BadRequestError", "UnsupportedParamsError"}:
        code = "parameters"
    elif category in {"ConnectionCapabilityError", "MaxTurnsExceeded", "ModelBehaviorError"}:
        code = "capability"
    return failure_for_code(code)


def shared_model_failure(errors) -> ConnectionFailure | None:
    """Only errors observed at the model port can halt other document work."""
    for error in errors:
        category = type(error).__name__
        if category in {"ModelBudgetExceeded", "ModelDeadlineExceeded"}:
            return ConnectionFailure(
                "execution_limit", "执行预算或时限已用尽；已提交结果保留，请手动重试剩余项。", True
            )
        if type(error).__module__.split(".")[0] not in {
            "litellm",
            "openai",
            "anthropic",
            "google",
            "botocore",
        }:
            continue
        failure = connection_failure(error)
        if failure.stops_batch:
            return failure
    return None
