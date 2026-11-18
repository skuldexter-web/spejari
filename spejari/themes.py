"""Colour themes shared by the CustomTkinter GUI and the Rich terminal dashboard.

Three themes ship with Spejari:

* ``sk7ld``  - Sk7ld: cyan blue and purple (the default)
* ``v1ru5``  - V1RU5: purple and lime green
* ``cvlt``   - cvlt of bahp0mau5: black, blood red, bone and tarnished silver, Baphomet style

A theme is plain data: every colour is a ``#rrggbb`` string that both Tk and Rich understand.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

# The maker's links, shown by every theme (GUI header + footer, terminal banner + dashboard header).
CREDIT_GITHUB = "github.com/skuldexter-web"
CREDIT_INSTAGRAM = "@s.k.7.l.d"
CREDIT_GITHUB_URL = "https://github.com/skuldexter-web"
CREDIT_INSTAGRAM_URL = "https://www.instagram.com/s.k.7.l.d/"


@dataclass(frozen=True)
class Theme:
    key: str  # value stored in the config file
    label: str  # name shown in the drop-down
    tagline: str  # shown under the logo
    glyph: str  # symbol next to the logo (GUI)
    cli_glyph: str  # symbol used by the terminal dashboard
    banner: str  # 3-line ASCII banner for the terminal
    # --- surfaces
    bg: str
    card: str
    card2: str
    btn: str  # neutral buttons / drop-downs
    btn_hover: str
    border: str
    # --- text
    text: str
    dim: str
    # --- brand colours
    accent: str  # primary highlight (logo, numbers, key values)
    accent2: str  # secondary (section titles, borders, headers)
    # --- semantic
    go: str  # start button
    go_hover: str
    go_text: str
    stop: str  # stop button
    stop_hover: str
    ok: str  # good signal / fix / scanning
    warn: str
    bad: str
    sel: str  # selected row in tables

    def credit_line(self) -> str:
        """'ᛋ github.com/skuldexter-web · IG @s.k.7.l.d ᛋ' in this theme's glyph."""
        return f"{self.cli_glyph} {CREDIT_GITHUB}  ·  IG {CREDIT_INSTAGRAM} {self.cli_glyph}"

    def rssi_color(self, rssi: int) -> str:
        if rssi >= -60:
            return self.ok
        if rssi >= -75:
            return self.warn
        return self.bad


_BANNER = "\n".join(
    [
        "╔═╗╔═╗╔═╗ ╦╔═╗╦═╗╦",
        "╚═╗╠═╝║╣  ║╠═╣╠╦╝║",
        "╚═╝╩  ╚═╝╚╝╩ ╩╩╚═╩",
    ]
)

# horned variant for the cult theme
_BANNER_CVLT = "\n".join(
    [
        "⛧ ╔═╗╔═╗╔═╗ ╦╔═╗╦═╗╦ ⛧",
        "ᛉ ╚═╗╠═╝║╣  ║╠═╣╠╦╝║ ᛉ",
        "⛧ ╚═╝╩  ╚═╝╚╝╩ ╩╩╚═╩ ⛧",
    ]
)

SK7LD = Theme(
    key="sk7ld",
    label="Sk7ld",
    tagline="cyan blue · purple · shield wall of the airwaves",
    glyph="ᛋ",
    cli_glyph="ᛋ",
    banner=_BANNER,
    bg="#080c1a",
    card="#0f1530",
    card2="#172044",
    btn="#252f63",
    btn_hover="#34408a",
    border="#6d4bd8",
    text="#dbe6ff",
    dim="#7f8bb8",
    accent="#22d3ee",
    accent2="#a78bfa",
    go="#0fb5d6",
    go_hover="#0a96b3",
    go_text="#04111a",
    stop="#8b3dff",
    stop_hover="#7027d8",
    ok="#2ee6c5",
    warn="#f0c75e",
    bad="#ff5c8a",
    sel="#27327a",
)

V1RU5 = Theme(
    key="v1ru5",
    label="V1RU5",
    tagline="purple · toxic lime · infection in progress",
    glyph="☣",
    cli_glyph="☣",
    banner=_BANNER,
    bg="#0b0612",
    card="#150b24",
    card2="#201036",
    btn="#38206a",
    btn_hover="#4e2d92",
    border="#7c3aed",
    text="#ece4ff",
    dim="#9684b8",
    accent="#a3e635",
    accent2="#b57bff",
    go="#84cc16",
    go_hover="#6aa80f",
    go_text="#0b1203",
    stop="#e11d74",
    stop_hover="#bb145e",
    ok="#a3e635",
    warn="#fde047",
    bad="#ff4d6d",
    sel="#3a1d6e",
)

CVLT = Theme(
    key="cvlt",
    label="cvlt of bahp0mau5",
    tagline="⛧ hail the signal · the horned one listens ⛧",
    glyph="ᛉ",
    cli_glyph="⛧",
    banner=_BANNER_CVLT,
    bg="#060404",
    card="#110b0b",
    card2="#1c1111",
    btn="#2e1717",
    btn_hover="#4a1d1d",
    border="#8c1c1c",
    text="#e6dccb",
    dim="#8f8173",
    accent="#d62828",
    accent2="#b9b0a0",
    go="#a4161a",
    go_hover="#7f1013",
    go_text="#f3e9d8",
    stop="#4a4540",
    stop_hover="#645d56",
    ok="#d9d0bf",
    warn="#c9a227",
    bad="#ff3b3b",
    sel="#3a1212",
)

THEMES: Dict[str, Theme] = {t.key: t for t in (SK7LD, V1RU5, CVLT)}
DEFAULT_THEME = "sk7ld"
_active: Theme = THEMES[DEFAULT_THEME]


def theme_keys() -> List[str]:
    return list(THEMES)


def theme_labels() -> List[str]:
    return [t.label for t in THEMES.values()]


def resolve(name: str) -> Theme:
    """Find a theme by key or by the label shown in the drop-down; unknown names fall back to the default."""
    needle = (name or "").strip().lower()
    for t in THEMES.values():
        if needle in (t.key, t.label.lower()):
            return t
    return THEMES[DEFAULT_THEME]


def set_active(name: str) -> Theme:
    global _active
    _active = resolve(name)
    return _active


def active() -> Theme:
    return _active
