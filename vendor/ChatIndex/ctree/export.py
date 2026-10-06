"""Pure, complete retrieval export; role narrowing cannot sanitize old topics."""
from .prompts import PROMPT_VERSION, allowed_turns
from .state import RebuildRequired, content_digests, prefix_digest, roles


def retrieval(tree, content_roles):
    content_roles = roles(content_roles)
    if not set(tree.analysis_roles).issubset(content_roles):
        raise RebuildRequired("Topics used excluded roles; rebuild with the desired analysis policy")
    turns = allowed_turns(tree.turns, content_roles)
    for original, exported in zip(tree.turns, turns):
        exported["content_digests"] = content_digests(original)
    return {"schema": "ctree.retrieval", "schema_version": 1,
            "conversation_id": tree.conversation_id,
            "basis": {"turn_count": len(turns), "turn_prefix_digest": prefix_digest(tree.turns),
                      "analysis_roles": list(tree.analysis_roles), "content_roles": list(content_roles),
                      "prompt_version": PROMPT_VERSION},
            "root": tree.root.to_dict(), "turns": turns}
