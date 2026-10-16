"""Plan with the sender's complete SDK input and the execution's frozen measure."""

from ultrakb.execution_capacity import capacity_policy, request_input
from ultrakb.llm_execution import CompletionExecutor, ModelRequest, RoleBindings, active_executor
from ultrakb.model_tokenizer import TokenMeasure


class CompileCapacity:
    def __init__(self, executor, measure):
        self.executor, self.measure = executor, measure
        if executor.compile_capacity is None:
            executor.compile_capacity = measure

    def payload(self, messages, options=None):
        from ultrakb.agent.compiler import _prepare_messages

        options = {"response_format": {"type": "json_object"}} if options is None else options
        request = ModelRequest(
            _prepare_messages(self.executor.bindings.model("compile"), messages),
            operation="compile",
            stage="capacity-planning",
            generation_options=options,
        )
        return request_input(self.executor.prepare_request("compile", request))

    def __call__(self, messages, options=None):
        payload = self.payload(messages, options or {"response_format": {"type": "json_object"}})
        if self.measure is None:
            from ultrakb.model_tokenizer import ContextCapacityError

            raise ContextCapacityError("Compile capacity is unknown; configure this connection")
        from ultrakb.request_budget import fits_request

        return fits_request(self.measure, payload)


def compile_capacity(kb_dir, model, bundle):
    from ultrakb.config import LlmCredentialBundle, resolve_effective_config
    from ultrakb.config_state import active_values

    executor = active_executor()
    if executor and executor.compile_capacity is not None:
        return CompileCapacity(executor, executor.compile_capacity)
    captured = active_values(kb_dir)
    config = captured["effective"] if captured else resolve_effective_config(kb_dir)[0]
    if executor:
        model, bundle = executor.bindings.model("compile"), executor.bindings.credentials()
    policy = capacity_policy(
        {**config, "model": model},
        custom_endpoint=bool(bundle and bundle.base_url),
        tokenizer_snapshot=captured.get("tokenizer_asset") if captured else None,
        base_url=bundle.base_url if bundle else None,
    )
    measure = None if policy["unknown_reason"] else TokenMeasure(policy)
    executor = executor or CompletionExecutor(
        RoleBindings(
            model,
            bundle or LlmCredentialBundle(),
            generation=config.get("role_generation"),
        )
    )
    return CompileCapacity(executor, measure)
