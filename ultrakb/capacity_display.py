"""Explain input measurement without changing the frozen processing identity."""


def capacity_description(policy: dict) -> str:
    from ultrakb.model_tokenizer import TokenMeasure

    try:
        unit = TokenMeasure(policy).unit
    except (ValueError, OSError, KeyError):
        unit = "unknown"
    limit = policy.get("input_limit") if not policy.get("unknown_reason") else None
    boundary = str(limit) if limit is not None else "未知"
    if unit == "utf8_byte":
        return (
            f"本地 UTF-8 字节护栏：{boundary} bytes。"
            "按完整请求的序列化字节计量；模型 token 上下文容量未由此确定。"
        )
    context, reserve = policy.get("context_window_tokens"), policy.get("output_reserve_tokens")
    estimator = "（使用 UTF-8 token 上界估算）" if unit == "token_upper_bound" else ""
    label = "默认模型输入预算" if context is not None else "模型输入界限"
    application = (
        f"应用请求字节护栏：{policy['max_request_bytes']} bytes。"
        if policy.get("max_request_bytes")
        else ""
    )
    return (
        f"{label}：{boundary} tokens{estimator}；模型 token 上下文："
        f"{context if context is not None else '未声明'}；输出预留："
        f"{reserve if reserve is not None else '未声明'} tokens。" + application
    )
