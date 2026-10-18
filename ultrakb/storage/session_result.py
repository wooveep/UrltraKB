"""Versioned display trace references into an immutable original assistant message."""


def reference_trace(trace, assistant):
    events = [dict(event) for event in trace]
    if assistant and events and events[-1].get("kind") == "text":
        text = events[-1].get("text", "")
        if text.endswith(assistant):
            events[-1] = {"kind": "assistant_original", "prefix": text[: -len(assistant)]}
    return {"schema": 1, "events": events}


def restore_trace(value, assistant):
    if (
        not isinstance(value, dict)
        or value.get("schema") != 1
        or not isinstance(value.get("events"), list)
    ):
        raise ValueError("Conversation display trace requires its versioned original reference")
    events = []
    for index, event in enumerate(value["events"]):
        if not isinstance(event, dict):
            raise ValueError("Conversation display trace contains an invalid event")
        if event.get("kind") == "assistant_original":
            if (
                set(event) != {"kind", "prefix"}
                or not isinstance(event["prefix"], str)
                or index != len(value["events"]) - 1
            ):
                raise ValueError("Conversation display trace has an invalid original reference")
            events.append({"kind": "text", "text": event["prefix"] + assistant})
        elif event.get("kind") in {"text", "tool"}:
            events.append(dict(event))
        else:
            raise ValueError("Conversation display trace contains an unknown event")
    return events
