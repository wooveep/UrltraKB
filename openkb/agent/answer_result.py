"""Present a rendered answer without mutating Agents SDK response objects."""


class RenderedAnswer:
    def __init__(self, result, answer):
        self.result = result
        self.final_output = answer

    def __getattr__(self, name):
        return getattr(self.result, name)

    def to_input_list(self):
        history = self.result.to_input_list()
        if history and history[-1].get("role") == "assistant":
            final = history[-1]
            content = self.final_output
            if isinstance(final.get("content"), list):
                content = [{"type": "output_text", "text": content}]
            history = [*history[:-1], {**final, "content": content}]
        return history
