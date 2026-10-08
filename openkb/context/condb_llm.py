"""ConDB's model protocol uses the captured OpenKB runtime and authoritative ledger."""

from contextdb.llm import _build_messages, _response_to_internal, _tools_to_openai

from openkb.llm_execution import CompletionExecutor, ModelRequest


class ConDBLLMAdapter:
    def __init__(self, executor: CompletionExecutor, role: str):
        if role not in {"knowledge_retrieval", "conversation_retrieval"}:
            raise ValueError("ConDB requires a retrieval model role")
        self.executor, self.role = executor, role

    def chat(self, messages, system="", tools=None, cache_key=None):
        purpose = (
            "Find knowledge-base originals using the published navigation. Directory/file "
            "names in the navigation protocol represent document sections and source locations. "
            "Select multiple relevant sources for comparisons. Navigation summaries locate "
            "evidence and do not establish facts."
            if self.role == "knowledge_retrieval"
            else "Locate past user intent. Navigation summaries only locate user turns; "
            "they do not certify what the user said or supply knowledge-base facts."
        )
        request = ModelRequest(
            _build_messages(messages, purpose + ("\n\n" + system if system else "")),
            operation=self.role,
            stage="beam",
            prompt_version="condb-beam-v1",
            tools=_tools_to_openai(tools) if tools else None,
            generation_options={"max_tokens": 1024, "temperature": 0, "drop_params": True},
            cache_segments=[0] if cache_key else [],
        )
        with self.executor.retrieval_slot():
            result = self.executor.complete(self.role, request)
        return _response_to_internal(result.response)

    def chat_with_cache(
        self,
        messages,
        system="",
        tools=None,
        cache_content=None,
        non_cached_content=None,
        cache_key=None,
    ):
        stable = cache_content if isinstance(cache_content, list) else [cache_content]
        content = "\n\n".join(s for s in [*stable, non_cached_content] if s)
        if content:
            messages = [{"role": "user", "content": content}, *messages]
        return self.chat(
            messages,
            system=system,
            tools=tools,
            cache_key=cache_key or ("stable" if cache_content else None),
        )
