"""Runtime construction for user-only conversation indexing."""

from ctree import CTree

from ultrakb.llm_execution import CompletionExecutor, ModelRequest, active_executor


class ChatIndexLLM:
    def __init__(self, executor: CompletionExecutor, *, responses=None):
        self.executor = executor
        self.responses = responses

    def complete(self, messages, *, stage, prompt_version):
        request = ModelRequest(
            messages,
            operation="conversation_index",
            stage=stage,
            prompt_version=prompt_version,
            generation_options={
                "response_format": {"type": "json_object"},
                "temperature": 0,
                "drop_params": True,
            },
        )
        if self.responses is not None:
            return self.responses.complete(self.executor, request)
        return self.executor.complete("conversation_index", request).text


def create_conversation_index(conversation_id, *, executor=None, max_children=5):
    executor = executor or active_executor()
    if executor is None:
        raise ValueError("Conversation indexing requires a task model runtime")
    return CTree(
        llm=ChatIndexLLM(executor),
        model=executor.bindings.model("conversation_index"),
        conversation_id=conversation_id,
        analysis_roles=("user",),
        max_children=max_children,
    )
