"""Use the pinned SDK's native conversion on one exact retained provider response."""

from agents.extensions.models.litellm_model import LitellmModel
from litellm import ModelResponse

from ultrakb.mutation import RecoveryRequired


class RetainedAgentResponse(LitellmModel):
    def __init__(self, executor, payload):
        try:
            choices = payload["choices"]
            if (
                not isinstance(choices, list)
                or len(choices) != 1
                or not isinstance(choices[0], dict)
                or choices[0].get("finish_reason") not in {"stop", "tool_calls"}
                or not isinstance(choices[0].get("message"), dict)
            ):
                raise ValueError("Expected one complete agent response")
            response = ModelResponse(**payload)
            usage = getattr(response, "usage", None)
            if usage is not None:
                # The SDK needs integers internally. The independent ledger
                # keeps the original unknown fields, as for _ReceiptModel.
                for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    if getattr(usage, name) is None:
                        setattr(usage, name, 0)
        except (KeyError, TypeError, ValueError) as error:
            raise RecoveryRequired(
                "The retained agent response requires explicit repair"
            ) from error
        super().__init__(model=executor.bindings.model("answer"))
        self.response = response

    async def _fetch_response(self, *args, **kwargs):
        # This path never starts a transport. get_response still uses the
        # actual SDK converter, preserving tool calls and provider metadata.
        return self.response
