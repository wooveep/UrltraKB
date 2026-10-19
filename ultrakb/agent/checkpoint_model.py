"""Accepted non-streaming agent responses for reads and rebuilding private output."""

from dataclasses import replace

from agents.exceptions import UserError
from agents.models.interface import ModelResponse
from agents.tool import FunctionTool
from pydantic import TypeAdapter

from ultrakb.compile_checkpoints import digest
from ultrakb.locks import LockCancelled
from ultrakb.mutation import RecoveryRequired
from ultrakb.storage.compile_binding import model_invocation
from ultrakb.storage.compile_deliveries import provider_response
from ultrakb.storage.coordination import storage_stage

from .managed_model import ManagedAgentModel
from .page_response import PageResponseError
from .retained_agent_response import RetainedAgentResponse


def raise_checkpoint_failure(error):
    """Recover our termination protocol from the SDK's tool error wrapper."""
    while isinstance(error, UserError) and error.__cause__ is not None:
        error = error.__cause__
    if isinstance(error, (LockCancelled, RecoveryRequired, PageResponseError)):
        raise error


class CheckpointAgentModel(ManagedAgentModel):
    def __init__(self, executor, work):
        super().__init__(executor)
        self.work = work
        self.sequence = 0

    async def get_response(
        self,
        system_instructions,
        input,
        model_settings,
        tools,
        output_schema,
        handoffs,
        tracing,
        **kwargs,
    ):
        if (
            handoffs
            or output_schema is not None
            or any(not isinstance(tool, FunctionTool) for tool in tools)
        ):
            raise ValueError("Maintenance response replay requires function tools and text output")
        signature = digest(
            {
                "protocol": "maintenance-agent-v1",
                "system_instructions": system_instructions,
                "input": input,
                "model_settings": replace(model_settings, retry=None).to_json_dict(),
                "tools": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.params_json_schema,
                        "strict": tool.strict_json_schema,
                    }
                    for tool in tools
                ],
                "options": kwargs,
            }
        )
        key = f"maintenance-agent:{self.sequence}"
        self.sequence += 1
        response_type = TypeAdapter(ModelResponse)
        retained = self.work.load(key, signature)
        if retained is not None:
            return response_type.validate_python(retained)
        arguments = (
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
        )
        with storage_stage(self.work.root, self.work.identity, read_only=True) as db:
            received = provider_response(db, self.work, key, signature)
        if received is not None:
            retained_model = RetainedAgentResponse(self.executor, received[0])
            try:
                response = await retained_model.get_response(*arguments, **kwargs)
            finally:
                await retained_model.close()
        else:
            invocation = self.work.claim_generation(key, signature, stage="maintenance.agent.v1")
            with model_invocation(self.work, invocation):
                response = await super().get_response(*arguments, **kwargs)
        try:
            self.work.save(
                key,
                signature,
                response_type.dump_python(response, mode="json", exclude_unset=True),
            )
        except LockCancelled:
            raise
        except Exception as error:
            raise RecoveryRequired(
                "The received agent response needs checkpoint acceptance"
            ) from error
        return response
