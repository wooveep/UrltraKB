"""Markdown boundaries at the agreed native content-rendering seam."""

from openkb.rendering.markdown import render_markdown
from openkb.rendering.renderer import RenderedBlock


class LocalRenderer:
    def __init__(self):
        self.sources = []

    def render(self, source, kind, **options):
        self.sources.append(source)
        return RenderedBlock(source, kind, options["display"], error="test renderer")


def test_code_and_link_destinations_remain_literal_while_real_math_is_rendered():
    renderer = LocalRenderer()
    result = render_markdown(
        "`[[concepts/demo]] $code$`\n\n"
        "```python\n[[concepts/demo]]\n$code$\n```\n\n"
        "    $indented$\n\n"
        "[link](https://example.test/$destination$)\n\n"
        "Actual $a$ and [[concepts/demo|page]].\n",
        renderer,
    )
    assert renderer.sources == ["a"]
    assert "[[concepts/demo]]" in result.html
    assert "https://example.test/$destination$" in result.html
    assert 'href="openkb:concepts/demo"' in result.html


def test_inline_display_formula_does_not_swallow_following_prose_or_blocks():
    renderer = LocalRenderer()
    result = render_markdown("$$x$$ plus text\n\nLater\n\n$$y$$\n", renderer)
    assert sorted(renderer.sources) == ["x", "y"]
    assert "plus text" in result.html and "Later" in result.html
