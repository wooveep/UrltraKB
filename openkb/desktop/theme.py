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
    background="#fafbfe",
    surface="#ffffff",
    sidebar="#f1f3f7",
    subtle="#f0f3f8",
    text="#151922",
    muted="#596579",
    border="#d8deea",
    accent="#2454ce",
    primary="#2563eb",
    primary_end="#1d4ed8",
    accent_hover="#1d4ed8",
    accent_pressed="#1e40af",
    on_accent="#ffffff",
    selection="#e4edff",
    hero_tint="#dce8ff",
    attention="#8a580d",
    attention_surface="#fff0d3",
    danger="#b43d3d",
)

DARK = Theme(
    background="#0b0d12",
    surface="#141821",
    sidebar="#080a0f",
    subtle="#1c2230",
    text="#f4f6fa",
    muted="#a0abbe",
    border="#30394b",
    accent="#8ab4ff",
    primary="#2563eb",
    primary_end="#1d4ed8",
    accent_hover="#1d4ed8",
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
