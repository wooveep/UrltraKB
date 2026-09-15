"""Show tool progress, then the completed terminal answer once."""

from contextlib import aclosing


async def terminal_answer(agent, input_data, style, *, use_color, raw, run_config=None):
    from openkb.agent.chat import _fmt, _format_tool_line, _make_markdown, _make_rich_console
    from openkb.agent.query import iter_agent_response_events

    stream = iter_agent_response_events(agent, input_data, run_config=run_config)
    async with aclosing(stream):
        async for event in stream:
            data = event["data"]
            if event["event"] == "tool_call":
                _fmt(
                    style, ("class:tool", _format_tool_line(data["name"], data["arguments"]) + "\n")
                )
            elif event["event"] == "final":
                # Draft deltas may contain partial short IDs or a rejected repair.
                # Keep final display identical to the answer and history being saved.
                if use_color and not raw:
                    _make_rich_console().print(_make_markdown(data["answer"]))
                else:
                    print(data["answer"])
                return data["answer"], data["history"]
    raise RuntimeError("The model did not return a final text answer")
