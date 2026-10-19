"""Seed explicit authored exchanges in the real native conversation store."""

from uuid import uuid4

from ultrakb.application.conversations import read_conversation
from ultrakb.application.views import published_scope
from ultrakb.storage.session_store import SessionStore


def seed_conversation(root, turns, *, model="deepseek/deepseek-flash", language="en"):
    scope = published_scope(root)
    store = SessionStore(root, scope.knowledge.identity)
    session_id = uuid4().hex
    store.create(session_id, model=model, language=language, view_id=scope.view_id)
    for user, answer in turns:
        store.append_exchange(session_id, uuid4().hex, user, answer)
    return read_conversation(root, session_id)
