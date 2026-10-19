"""PageIndex indexer for long documents."""

from __future__ import annotations

import logging
from typing import Any

from pageindex import IndexConfig

from ultrakb.config import (
    LlmCredentialBundle,
    resolve_concurrency,
    resolve_per_request_overrides,
)

logger = logging.getLogger(__name__)


def _build_index_config(
    config: dict[str, Any], *, bundle: LlmCredentialBundle | None = None
) -> IndexConfig:
    """Build the PageIndex ``IndexConfig`` for local indexing.

    Forwards the KB's ``concurrency`` setting to PageIndex, which caps how many
    indexing LLM calls run at once (guarding against "too many open files" fd
    exhaustion on large documents). The value is only passed when set *and* the
    installed PageIndex's ``IndexConfig`` declares the field, so UltraKB keeps
    working against a pinned PageIndex that predates it (``IndexConfig``
    forbids unknown kwargs).
    """
    kwargs: dict[str, Any] = {
        **config.get("index_navigation", {}),
        "if_add_node_text": True,
        "if_add_node_summary": True,
        "if_add_doc_description": True,
    }
    from ultrakb.index_llm import PageIndexLLM
    from ultrakb.llm_execution import compiler_executor

    headers, timeout, _ = resolve_per_request_overrides(config)
    from ultrakb.llm_runtime import audit_step_headers

    headers = audit_step_headers(headers, "pageindex.index")
    params: dict[str, Any] = {}
    if timeout is not None:
        params["timeout"] = timeout
    if headers:
        params["extra_headers"] = headers
    if bundle is not None:
        if bundle.api_key:
            params["api_key"] = bundle.api_key
        if bundle.base_url:
            params["api_base"] = bundle.base_url
    if params:
        # PageIndex scopes these per index; LiteLLM globals cannot override its
        # own timeout default. Forward the same resolved KB settings as compile.
        kwargs["llm_params"] = params
    executor_options = {"extra_headers": headers, "timeout": timeout}
    executor = compiler_executor(config.get("model", "gpt-5.4"), bundle, executor_options)
    if executor.index_capacity_policy is None:
        from ultrakb.execution_capacity import capacity_policy

        executor.index_capacity_policy = capacity_policy(
            {**config, "model": executor.bindings.model("document_index")},
            custom_endpoint=bool(executor.bindings.credentials().base_url),
            base_url=executor.bindings.credentials().base_url,
        )
    kwargs["llm_client"] = PageIndexLLM(executor)
    kwargs["require_llm_client"] = True
    concurrency = resolve_concurrency(config)
    if concurrency is not None:
        if "max_concurrency" in IndexConfig.model_fields:
            kwargs["max_concurrency"] = concurrency
        else:
            logger.warning(
                "config: 'concurrency' is set but the installed PageIndex "
                "version does not support it yet — ignoring it."
            )
    return IndexConfig(**kwargs)
