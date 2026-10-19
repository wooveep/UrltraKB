"""Display traces preserve final answers and do not duplicate streamed text."""

from ultrakb.agent.chat_trace import append_event, complete_trace


def test_whitespace_only_delta_then_final_answer_lands_in_trace():
    trace = []
    append_event(trace, {"event": "delta", "data": {"text": " "}})
    append_event(trace, {"event": "delta", "data": {"text": "\n"}})
    complete_trace(trace, "The retained answer.")
    assert any(
        step.get("kind") == "text" and step.get("text") == "The retained answer." for step in trace
    )


def test_substantive_streamed_text_is_not_duplicated_by_final():
    trace = []
    append_event(trace, {"event": "delta", "data": {"text": "The retained "}})
    append_event(trace, {"event": "delta", "data": {"text": "answer."}})
    complete_trace(trace, "The retained answer.")
    assert [step for step in trace if step.get("kind") == "text"] == [
        {"kind": "text", "text": "The retained answer."}
    ]
