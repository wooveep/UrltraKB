"""Role-filtered prompt templates; all stage inputs pass this single boundary."""
import json

PROMPT_VERSION = "chat-topics-v1"
INSTRUCTIONS = {
    "name": 'Name the subject in 2-6 words. Return JSON {"title": "..."}.',
    "classify": 'Choose whether the new exchange continues the current topic. Return JSON '
                '{"belongs_to_current": true} or {"belongs_to_current": false, '
                '"title": "...", "parent_id": "one of the ancestor IDs"}.',
    "split": 'Group consecutive children into exactly two nonempty subtopics. Return JSON '
             '{"groups": [{"title": "...", "start": 0, "end": K}, '
             '{"title": "...", "start": K, "end": N}]}. Ranges index children, '
             'are half-open, and must cover all N children without overlap or gaps.',
    "summary": 'Summarize the allowed conversation content in 1-2 sentences. '
               'Return JSON {"summary": "..."}.',
    "frozen_summary": 'Summarize this completed topic using only the supplied content. '
                      'Return JSON {"summary": "..."}.',
}


def allowed_turns(turns, roles):
    return [{"turn_id": t["turn_id"], "ordinal": t["ordinal"],
             "messages": [dict(m) for m in t["messages"] if m["role"] in roles]} for t in turns]


def render(stage, payload, roles, repair=False):
    instruction = INSTRUCTIONS[stage]
    if repair:
        instruction += " The previous response had an invalid format. Follow the JSON contract exactly."
    return [{"role": "user", "content": instruction + "\nAnalysis roles: " + ", ".join(roles)
             + "\n" + json.dumps(payload, ensure_ascii=False, sort_keys=True)}]
