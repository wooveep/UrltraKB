"""Track complete transport frames independently of provider fee settlement."""

import copy
import json


class StreamCapture:
    def __init__(self):
        self.chunks = []
        self.usage = None
        self.done = False
        self.failed = False
        self._choices = set()
        self._finished = set()

    def accept(self, value):
        if self.done:
            return None
        if value == "[DONE]":
            self.done = True
            return None
        try:
            chunk = json.loads(value)
            if not isinstance(chunk, dict) or not isinstance(chunk.get("choices"), list):
                raise ValueError("SSE frame must contain a chunk with choices")
            for choice in chunk["choices"]:
                if not isinstance(choice, dict) or type(choice.get("index")) is not int:
                    raise ValueError("SSE chunk choices require integer indexes")
                self._choices.add(choice["index"])
                if choice.get("finish_reason") is not None:
                    self._finished.add(choice["index"])
            usage = chunk.get("usage")
            if isinstance(usage, dict):
                if self.usage is not None and usage != self.usage:
                    raise ValueError("SSE chunk contains conflicting final usage receipts")
                self.usage = copy.deepcopy(usage)
            self.chunks.append(copy.deepcopy(chunk))
            return chunk
        except Exception:
            self.failed = True
            raise

    @property
    def cacheable(self):
        return bool(
            self.done
            and not self.failed
            and self.usage is not None
            and self._choices
            and self._choices == self._finished
        )
