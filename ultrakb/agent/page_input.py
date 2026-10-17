"""The complete page input, assembled identically during planning and sending."""


def page_input(messages, *, figures=None, excerpts=None, structured=False, compact=False):
    result = list(messages)
    if figures is not None:
        from ultrakb.agent.compile_contract import request_version

        current = getattr(excerpts, "request_version", request_version())
        message = figures.message(compact=compact, bounded_catalogue=compact and current >= 3)
        if message not in result:
            result.append(message)
    if excerpts is not None:
        result = excerpts.messages(result, structured=structured, compact=compact)
    return result
