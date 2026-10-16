"""The complete page input, assembled identically during planning and sending."""


def page_input(messages, *, figures=None, excerpts=None, structured=False, compact=False):
    result = list(messages)
    if figures is not None:
        message = figures.message(compact=compact)
        if message not in result:
            result.append(message)
    if excerpts is not None:
        result = excerpts.messages(result, structured=structured, compact=compact)
    return result
