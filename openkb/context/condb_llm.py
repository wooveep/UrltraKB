"""ConDB's model protocol uses the captured OpenKB runtime and authoritative ledger."""

import json

from contextdb.llm import _build_messages, _response_to_internal, _tools_to_openai

from openkb.llm_execution import CompletionExecutor, ModelRequest


class RetrievalFailure(ValueError):
    """A deterministic ranking failure, never evidence that a source is absent."""

    def __init__(self, code: str):
        self.code = code
        self.consumed_sends = 0
        super().__init__(code)


def completed_rank(result, tools):
    # Never inspect/repair partial JSON, including apparently complete rank calls.
    if result.finish_reason == "length":
        raise RetrievalFailure("output_truncated")
    if result.finish_reason not in {"stop", "tool_calls"}:
        raise RetrievalFailure("completion_incomplete")
    rank = next((tool for tool in tools or [] if tool["name"] == "rank"), None)
    if rank:
        calls = [call for call in result.tool_calls if call.function.name == "rank"]
        if len(calls) != 1:
            raise RetrievalFailure("rank_missing" if not calls else "rank_invalid")
        try:
            args = json.loads(calls[0].function.arguments)
        except (ValueError, TypeError) as error:
            raise RetrievalFailure("rank_invalid_json") from error
        if (
            not isinstance(args, dict)
            or not isinstance(args.get("ranked_ids"), list)
            or any(not isinstance(identity, str) for identity in args["ranked_ids"])
            or type(args.get("done")) is not bool
            or len(set(args["ranked_ids"])) != len(args["ranked_ids"])
        ):
            raise RetrievalFailure("rank_invalid")
        allowed = rank["input_schema"]["properties"]["ranked_ids"]["items"].get("enum")
        if allowed is not None and any(identity not in allowed for identity in args["ranked_ids"]):
            raise RetrievalFailure("candidate_outside_selection")
    try:
        return _response_to_internal(result.response)
    except (ValueError, TypeError) as error:
        raise RetrievalFailure("rank_invalid_json") from error


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
        with self.executor.retrieval_slot():
            for attempt in range(2):
                output = (
                    self.executor.policy.retrieval_repair_tokens
                    if attempt
                    else self.executor.policy.retrieval_tokens
                )
                request = ModelRequest(
                    _build_messages(messages, purpose + ("\n\n" + system if system else "")),
                    operation=self.role,
                    stage="beam",
                    prompt_version="condb-beam-v2",
                    tools=_tools_to_openai(tools) if tools else None,
                    generation_options={"max_tokens": output, "drop_params": False},
                    cache_segments=[0] if cache_key else [],
                )
                measure = self.executor.question_capacity
                if measure is not None:
                    window = measure.policy.get("context_window_tokens")
                    if window and measure.payload_tokens(request.payload()) + output > window:
                        raise RetrievalFailure("retrieval_context_capacity")
                result = self.executor.complete(self.role, request)
                try:
                    return completed_rank(result, tools)
                except RetrievalFailure:
                    if attempt or not self.executor.allow_retrieval_repair():
                        raise
        raise AssertionError("Unreachable ranking repair state")

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
