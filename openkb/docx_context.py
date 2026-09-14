"""Original DOCX cell excerpts without display-only notices or attachment markers."""


def cell_excerpts(cell, *, notes, comments, attachments):
    """Walk original nodes without OCR, storage writes, or generated display wrappers."""
    from mammoth import documents as nodes

    result, active = [], set()
    separate = True

    def append(text, kind):
        nonlocal separate
        if not text:
            return
        if not separate and result and result[-1].get("source_kind") == kind:
            result[-1]["text"] += text
        else:
            result.append({"text": text, **({"source_kind": kind} if kind else {})})
        separate = False

    def visit(node, kind=None):
        nonlocal separate
        boundary = isinstance(
            node,
            (
                nodes.Paragraph,
                nodes.Table,
                nodes.Image,
                nodes.NoteReference,
                nodes.CommentReference,
            ),
        )
        if boundary:
            separate = True
        if isinstance(node, nodes.Text):
            if node.value not in attachments:
                append(node.value, kind)
            else:
                separate = True
        elif isinstance(node, nodes.Tab):
            append("\t", kind)
        elif isinstance(node, nodes.Break):
            append("\n", kind)
        elif isinstance(node, nodes.Image):
            append(node.alt_text, "image_alt")
        elif isinstance(node, nodes.NoteReference):
            identity = (node.note_type, node.note_id)
            if notes is None or identity in active:
                return
            try:
                note = notes.resolve(node)
            except KeyError:
                return  # The display reader has already reported the missing original.
            active.add(identity)
            try:
                for child in note.body:
                    visit(child, node.note_type)
            finally:
                active.remove(identity)
        elif isinstance(node, nodes.CommentReference):
            identity = ("comment", node.comment_id)
            comment = comments.get(node.comment_id)
            if comment is not None and identity not in active:
                active.add(identity)
                try:
                    for child in comment.body:
                        visit(child, "editorial_comment")
                finally:
                    active.remove(identity)
        else:
            for child in getattr(node, "children", []):
                visit(child, kind)
        if boundary:
            separate = True

    visit(cell)
    return result
