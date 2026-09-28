"""Black, white and blue colors shared by native chrome and reading surfaces."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    background: str
    surface: str
    sidebar: str
    subtle: str
    text: str
    muted: str
    border: str
    accent: str
    primary: str
    primary_end: str
    accent_hover: str
    accent_pressed: str
    on_accent: str
    selection: str
    hero_tint: str
    attention: str
    attention_surface: str
    danger: str


LIGHT = Theme(
    background="#f5f7fb",
    surface="#ffffff",
    sidebar="#ffffff",
    subtle="#f0f3f9",
    text="#202938",
    muted="#647086",
    border="#e1e6ef",
    accent="#365bd6",
    primary="#365bd6",
    primary_end="#294bc1",
    accent_hover="#294bc1",
    accent_pressed="#1e40af",
    on_accent="#ffffff",
    selection="#eaf0ff",
    hero_tint="#edf2ff",
    attention="#8a580d",
    attention_surface="#fff0d3",
    danger="#b43d3d",
)

DARK = Theme(
    background="#10141d",
    surface="#181e2a",
    sidebar="#141923",
    subtle="#1c2230",
    text="#f4f6fa",
    muted="#a0abbe",
    border="#2c3545",
    accent="#8ab4ff",
    primary="#365bd6",
    primary_end="#294bc1",
    accent_hover="#294bc1",
    accent_pressed="#1e40af",
    on_accent="#ffffff",
    selection="#1b2d50",
    hero_tint="#192f62",
    attention="#efc178",
    attention_surface="#3c3020",
    danger="#f19393",
)


def theme_colors(dark: bool = False) -> Theme:
    return DARK if dark else LIGHT
