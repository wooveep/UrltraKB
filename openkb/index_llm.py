"""PageIndex's narrow runtime adapter; no vendor dependency on OpenKB."""

from pageindex.llm import IndexResponse

from openkb.llm_execution import CompletionExecutor, ModelRequest


class PageIndexLLM:
    def __init__(self, executor: CompletionExecutor):
        self.executor = executor

    def _request(self, messages, stage):
        return ModelRequest(
            messages,
            operation="document_index",
            stage=stage,
            prompt_version="pageindex-content-v1",
            generation_options={"temperature": 0, "drop_params": True},
        )

    @staticmethod
    def _response(result):
        return IndexResponse(
            result.text, "max_output_reached" if result.finish_reason == "length" else "finished"
        )

    def complete(self, messages, *, stage):
        return self._response(
            self.executor.complete("document_index", self._request(messages, stage))
        )

    async def acomplete(self, messages, *, stage):
        return self._response(
            await self.executor.acomplete("document_index", self._request(messages, stage))
        )
