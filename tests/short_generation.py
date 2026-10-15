"""Small retained inputs for genuine artifact generation through every adapter."""

import json

from ultrakb.locks import atomic_write_text

FORMATTER_SOURCE = (
    "Bullet-list formatting: prefix each supplied line with a hyphen "
    "and a space; retain its original text and order."
)
FORMATTER_INTENT = (
    "Until done has been called, emit only tool calls and no assistant text: "
    "no drafts, reviews, word or character counts, plans or narration. "
    "Create a tiny bullet-list formatter skill. Read index.md once, then read the sole original "
    "with get_text_content(doc_name='formatter', chars='0:{end}', offset=0). "
    "This direct read covers the whole source; do not use search_knowledge or query_wiki. "
    "The complete SKILL.md, including frontmatter, must fit 50 words and 600 characters. "
    "Frontmatter has only name and a description of at most five words. "
    "Keep the required sections, using a single short phrase per section. "
    "Use exactly eight core rules, each at most three words, derived only from the "
    "source's prefix, original-text and order rules. "
    "No examples, references, extra rules or supporting files. "
    "Write SKILL.md in one write_skill_file call. "
    "Then call done once with at most ten words; only afterwards emit a final answer "
    "of at most five words."
)
MINIMAL_DECK_HTML = (
    "<!doctype html><html><body>"
    + "".join(
        f'<section class="slide">{text}</section>'
        for text in ("Orion 17 credits", "Plan", "Track", "Approve", "Done")
    )
    + "</body></html>"
)


def configure_generation(kb, *, max_tokens=512):
    """Bound production sends and output, including default 80-turn SDK callers."""
    atomic_write_text(
        kb / ".ultrakb/config.yaml",
        "model: deepseek/deepseek-flash\nlanguage: en\nmodel_capacity:\n"
        "  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\nmodel_policy:\n"
        "  max_calls: 6\n  retries: 0\n  deadline_seconds: 60\nrole_generation:\n"
        f"  answer:\n    max_tokens: {max_tokens}\n    reasoning_effort: none\n",
    )


def retained_formatter(kb, credentials, *, recorded_answer=None):
    """Publish supplied original input with zero sends; generation still uses real LLM I/O."""
    from publication_fixtures import publish_document

    configure_generation(kb)
    source = FORMATTER_SOURCE
    if recorded_answer is not None:
        source += f" The recorded answer is {recorded_answer}."
    original = kb / "formatter.txt"
    atomic_write_text(original, source)
    published = publish_document(kb, original, source, bundle=credentials)
    assert published.status == "added", published.message
    return FORMATTER_INTENT.format(end=len(source))


def install_short_deck(kb):
    """Use a KB-local, minimal skill through the production default-skill lookup."""
    from ultrakb.deck import DEFAULT_DECK_SKILL

    configure_generation(kb, max_tokens=256)
    atomic_write_text(kb / "wiki/concepts/topic.md", "# Topic\nOrion budget: 17 credits.")
    atomic_write_text(kb / "wiki/index.md", "# Index\nOrion budget: 17 credits.")
    atomic_write_text(
        kb / f"skills/{DEFAULT_DECK_SKILL}/SKILL.md",
        f"---\nname: {DEFAULT_DECK_SKILL}\ndescription: Produce compact budget decks\n"
        "od:\n  mode: deck\n  output_path_template: output/decks/{slug}/index.html\n"
        "---\nUse the supplied HTML in a single write_file call, then finish. "
        "Do not add assets or supporting files.",
    )
    return "Use this user-supplied HTML exactly, then finish: " + MINIMAL_DECK_HTML


def assert_formatter_delivery(kb, name, service):
    """Check actual artifact facts and original content delivered to the real model."""
    from ultrakb import frontmatter
    from ultrakb.skill.validator import validate_skill

    target = kb / f"output/skills/{name}"
    text = (target / "SKILL.md").read_text()
    metadata = frontmatter.parse(text)
    assert metadata["name"] == name and metadata["description"]
    assert "bullet" in text.lower()
    assert "hyphen" in text.lower() or "- " in text
    assert "text" in text.lower() and "order" in text.lower()
    assert not validate_skill(target).errors
    assert 2 <= len(service.requests) <= 6
    primary = []
    for request in service.requests:
        tools = {tool["function"]["name"] for tool in request.get("tools", [])}
        if "write_skill_file" in tools:
            primary.append(request)
        # The actual ConDB beam protocol has a separate 1024-token rank
        # allowance; all answer-agent requests use the captured 512-token cap.
        assert request["max_tokens"] == (1024 if "rank" in tools else 512)
    assert primary and all(request["max_tokens"] == 512 for request in primary)
    delivered = [
        message
        for request in service.requests
        for message in request["messages"]
        if message["role"] == "tool"
    ]
    assert "prefix each supplied line" in json.dumps(delivered)
    return text
