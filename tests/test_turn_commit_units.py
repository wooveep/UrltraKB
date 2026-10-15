"""Rules for committed exchanges; all text below is authored domain input."""

import pytest

from ultrakb.conversation_turns import ConversationTurn, exchange_digest, load_turns

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("assistant_text", ["", " \n\t"])
def test_an_unfinished_answer_cannot_become_a_committed_exchange(assistant_text):
    with pytest.raises(ValueError, match="committed exchange requires.*text"):
        ConversationTurn.create(1, "Preserve my exact submitted question.", assistant_text)


@pytest.mark.parametrize("user_text", ["", " \n\t"])
def test_a_missing_question_cannot_become_a_committed_exchange(user_text):
    with pytest.raises(ValueError, match="committed exchange requires.*text"):
        ConversationTurn.create(1, user_text, "An authored historical answer.")


def test_committing_keeps_the_original_whitespace_and_line_breaks():
    turn = ConversationTurn.create(1, "  原文问题\n第二行  ", "\n  人工核对记录\n")

    assert turn.user_text == "  原文问题\n第二行  "
    assert turn.assistant_text == "\n  人工核对记录\n"


@pytest.mark.parametrize("assistant_text", ["", " \n\t"])
def test_restore_rejects_an_unfinished_answer_even_with_a_matching_digest(assistant_text):
    question = "The original submission is still pending."
    saved = {
        "turn_id": "retained-turn",
        "order": 1,
        "user_text": question,
        "assistant_text": assistant_text,
        "content_digest": exchange_digest(question, assistant_text),
        "attempt_id": "retained-attempt",
    }

    with pytest.raises(ValueError, match="committed exchange requires.*text"):
        load_turns([saved])
