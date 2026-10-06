"""Compatibility helpers with explicit provider configuration and no retry loop."""
import json


def ChatGPT_API(model, prompt, api_key=None, chat_history=None, temperature=0, max_tokens=None):
    from .llm import create_client
    if api_key is None:
        raise ValueError("Provide explicit credentials or inject ChatLLM")
    return create_client(model=model, api_key=api_key).complete(
        [*(chat_history or []), {"role": "user", "content": prompt}],
        stage="compatibility", prompt_version="chat-topics-v1")


def extract_json(content):
    return json.loads(content)
