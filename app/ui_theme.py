"""Theming: a registry of selectable skins + the single application stylesheet.

Skins work like old Winamp skins — the user picks one from a dropdown and the
whole chrome re-colors live. Every color in the app (QSS chrome AND the Python-
side console text formats) reads from the module-level `Palette`; `apply_theme`
rewrites Palette's attributes (and `ANSI_16`, the font globals) IN PLACE, so all
existing `Palette.X` / `ANSI_16` references across the app pick up the new theme
with no per-call-site changes. Callers then rebuild the QSS and repolish.

Adding a skin = adding one `Theme(...)` to `THEMES`. Nothing else.
"""

import os
import tempfile
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Theme:
    id: str
    name: str            # shown in the dropdown
    # --- surfaces ---
    bg_root: str         # app background / grid ground
    bg_panel: str        # sidebar / top bar / activity chrome
    bg_card: str         # terminal card body frame
    bg_console: str      # console/terminal viewport
    bg_input: str
    bg_hover: str
    bg_active: str       # active sidebar row
    border: str
    border_soft: str
    # --- accents ---
    accent_gold: str     # primary chrome accent
    accent_gold_dim: str
    accent_focus: str    # focused-card border (orange in dark, ultramarine in ms)
    selection: str
    green: str
    red: str
    yellow: str
    # --- text ---
    text: str            # primary chrome text
    text_dim: str
    text_faint: str
    console_fg: str
    input_echo: str
    system_msg: str
    selection_fg: str    # text color ON a selection highlight (contrast-safe)
    # --- card running-head (title bar) ---
    bg_cardhead: str
    cardhead_fg: str
    cardhead_sub: str
    appname_fg: str      # top-bar wordmark color
    scroll_hover: str    # scrollbar handle hover
    # --- fonts ---
    display_font: str    # titles / wordmark (serif inscriptional in manuscript)
    body_font: str       # chrome labels / names
    console_font: str    # terminals (always mono)
    # --- 16-colour ANSI (tuned to bg_console) ---
    ansi: tuple
    # --- flags ---
    light: bool = False    # light chrome (affects a few QSS choices)
    ornament: str = ""     # painter set: "manuscript" | "mechanicus" | ""


_CONSOLA = 'Consolas, "Cascadia Mono", monospace'

# ANSI tuned for a near-black console (the classic dark set).
_ANSI_DARK = (
    "#3b3b3b", "#f44747", "#6a9955", "#dcdcaa",
    "#569cd6", "#c586c0", "#4fc1ff", "#d4d4d4",
    "#6b6b6b", "#ff6b6b", "#8ec07c", "#f0e68c",
    "#74b0f1", "#d8a0df", "#7fd8ff", "#f2f2f2",
)
# ANSI tuned to read on a LIGHT vellum background (darker, saturated inks).
_ANSI_VELLUM = (
    "#5a4636", "#a3272b", "#3f6d34", "#8a6a12",
    "#1f4a8f", "#7a3d86", "#1d6a86", "#2b1f0f",
    "#7c6450", "#b62e2c", "#4f7d3f", "#a9822a",
    "#22539c", "#9c5ea6", "#2b7fa0", "#000000",
)

# --------------------------------------------------------------- registry ---

THEMES: dict[str, Theme] = {
    # The shipped look, unchanged — dark warm parchment + illuminated gold.
    "scriptorium-dark": Theme(
        id="scriptorium-dark", name="Scriptorium (Dark)",
        bg_root="#12100c", bg_panel="#17140e", bg_card="#1b1712",
        bg_console="#0d0d0d", bg_input="#151209", bg_hover="#24201a",
        bg_active="#2a2212", border="#2c2822", border_soft="#211d17",
        accent_gold="#c9a227", accent_gold_dim="#8a7320", accent_focus="#e8983a",
        selection="#3b82f6", green="#5fae6b", red="#d1564f", yellow="#d9b24a",
        text="#ece3d0", text_dim="#9a9080", text_faint="#6a6153",
        console_fg="#c9d1d9", input_echo="#d9b24a", system_msg="#9a9080",
        selection_fg="#ffffff",
        bg_cardhead="#17140e", cardhead_fg="#ece3d0", cardhead_sub="#9a9080",
        appname_fg="#ece3d0", scroll_hover="#3d3d3d",
        display_font='"Constantia", "Cambria", "Georgia", serif',
        body_font='"Segoe UI", sans-serif', console_font=_CONSOLA,
        ansi=_ANSI_DARK),

    # The illuminated-manuscript design: light vellum chrome, ultramarine
    # running-heads, gold rules, parchment terminals. Ornaments in Phase B.
    "illuminated-manuscript": Theme(
        id="illuminated-manuscript", name="Illuminated Manuscript",
        bg_root="#e6d7b1",       # vellum deep (page ground behind folios)
        bg_panel="#f4ead0",      # vellum light (rail / top bar / activity)
        bg_card="#f7efd6",       # folio frame
        bg_console="#fbf4df",    # parchment terminal
        bg_input="#fcf6e4",
        bg_hover="#ecdfbf",
        bg_active="#efe0b8",
        border="#c39430",        # gold border
        border_soft="#cdb583",
        accent_gold="#b98a2e", accent_gold_dim="#9c7a3a",
        accent_focus="#173571",  # focused folio → ultramarine
        selection="#c9a94e",
        green="#3f6d34", red="#8a1f22", yellow="#8a611c",
        text="#2b1f0f",          # ink
        text_dim="#6b4e22", text_faint="#9c7a3a",
        console_fg="#2b1f0f",    # ink on parchment
        input_echo="#8a1f22",    # vermillion echo
        system_msg="#7a5e34",
        selection_fg="#2b1f0f",  # dark ink on the light gold selection

        bg_cardhead="#173571",   # ultramarine running-head
        cardhead_fg="#f0d777",   # gold title
        cardhead_sub="#a9bce0",
        appname_fg="#173571",    # ultramarine wordmark
        scroll_hover="#c39430",
        display_font='"Cinzel Decorative", "Cinzel", "Constantia", "Georgia", serif',
        body_font='"Spectral", "EB Garamond", "Georgia", serif',
        console_font=_CONSOLA,
        ansi=_ANSI_VELLUM, light=True, ornament="manuscript"),

    # Warhammer 40K Adeptus Mechanicus cogitator: green-phosphor CRT on
    # brushed gunmetal, amber telemetry. Dark, so terminals read fine.
    "adeptus-mechanicus": Theme(
        id="adeptus-mechanicus", name="Adeptus Mechanicus",
        bg_root="#070a09",        # desk backdrop
        bg_panel="#0a1f17",       # rail / top bar (green glass)
        bg_card="#0a2418",        # agent cell frame
        bg_console="#05140d",     # terminal glass (near-black green)
        bg_input="#041009",
        bg_hover="#123a2b",
        bg_active="#103528",
        border="#1d5a44",         # phosphor-green border
        border_soft="#164536",
        accent_gold="#5fe8ac", accent_gold_dim="#2f9a76",  # phosphor accents
        accent_focus="#5fe8ac",   # focused cell → bright phosphor
        selection="#3a9a78",
        green="#5fe8ac", red="#e0554a", yellow="#e0a838",  # amber = yellow slot
        text="#a7ecc9",           # phosphor body
        text_dim="#4fbf94", text_faint="#2f9a76",
        console_fg="#a7ecc9",     # phosphor terminal text
        input_echo="#6effc0",     # bright green echo
        system_msg="#4fbf94",
        selection_fg="#04180f",   # near-black on the green selection
        bg_cardhead="#0e3a2b",    # running-head green
        cardhead_fg="#9dffcf",    # bright phosphor title
        cardhead_sub="#e0a838",   # amber "machine spirit" eyebrow
        appname_fg="#9dffcf",     # phosphor wordmark
        scroll_hover="#2f9a76",
        display_font='"Cinzel Decorative", "Cinzel", "Constantia", "Georgia", serif',
        body_font='"Spectral", "EB Garamond", "Georgia", serif',
        console_font='"Share Tech Mono", Consolas, monospace',
        ansi=_ANSI_DARK, ornament="mechanicus"),

    # A clean modern dark option (neutral slate + blue), standard dark ANSI.
    "obsidian": Theme(
        id="obsidian", name="Obsidian",
        bg_root="#0f1117", bg_panel="#161922", bg_card="#1a1e28",
        bg_console="#0b0d12", bg_input="#12151d", bg_hover="#232836",
        bg_active="#1e2735", border="#2a2f3c", border_soft="#20242f",
        accent_gold="#5aa0ff", accent_gold_dim="#3f6ea8", accent_focus="#5aa0ff",
        selection="#3b82f6", green="#4ec9b0", red="#f14c4c", yellow="#d7ba7d",
        text="#d6dae2", text_dim="#8b93a3", text_faint="#5c6373",
        console_fg="#d6dae2", input_echo="#5aa0ff", system_msg="#8b93a3",
        selection_fg="#ffffff",
        bg_cardhead="#161922", cardhead_fg="#d6dae2", cardhead_sub="#8b93a3",
        appname_fg="#d6dae2", scroll_hover="#3a4152",
        display_font='"Segoe UI Semibold", "Segoe UI", sans-serif',
        body_font='"Segoe UI", sans-serif', console_font=_CONSOLA,
        ansi=_ANSI_DARK),
}

DEFAULT_THEME_ID = "scriptorium-dark"


# ------------------------------------------------------ live-mutable state ---
# Populated from the active Theme by apply_theme(); read everywhere as before.

class Palette:
    """Active theme's colors. Attributes are rewritten by apply_theme() so all
    existing `Palette.X` reads across the app follow the selected skin.
    ACCENT_BLUE is a legacy alias kept pointing at the primary accent."""
    pass


ANSI_16: list = list(_ANSI_DARK)   # mutated in place (kept as the same object)
DISPLAY_FONT = THEMES[DEFAULT_THEME_ID].display_font
BODY_FONT = THEMES[DEFAULT_THEME_ID].body_font
CONSOLE_FONT = THEMES[DEFAULT_THEME_ID].console_font
ACTIVE_THEME: Theme = THEMES[DEFAULT_THEME_ID]

DEFAULT_CONSOLE_PX = 13
CONSOLE_FONT_PX = DEFAULT_CONSOLE_PX


def apply_theme(theme_id: str) -> Theme:
    """Make `theme_id` the active skin: rewrite Palette / ANSI_16 / fonts in
    place. Returns the Theme. Unknown id falls back to the default."""
    global DISPLAY_FONT, BODY_FONT, CONSOLE_FONT, ACTIVE_THEME
    t = THEMES.get(theme_id) or THEMES[DEFAULT_THEME_ID]
    Palette.BG_ROOT = t.bg_root
    Palette.BG_PANEL = t.bg_panel
    Palette.BG_CARD = t.bg_card
    Palette.BG_CONSOLE = t.bg_console
    Palette.BG_INPUT = t.bg_input
    Palette.BG_HOVER = t.bg_hover
    Palette.BG_ACTIVE = t.bg_active
    Palette.BORDER = t.border
    Palette.BORDER_SOFT = t.border_soft
    Palette.ACCENT_GOLD = t.accent_gold
    Palette.ACCENT_GOLD_DIM = t.accent_gold_dim
    Palette.ACCENT_BLUE = t.accent_gold   # legacy alias → primary accent
    Palette.ACCENT_ORANGE = t.accent_focus
    Palette.SELECTION = t.selection
    Palette.GREEN = t.green
    Palette.RED = t.red
    Palette.YELLOW = t.yellow
    Palette.TEXT = t.text
    Palette.TEXT_DIM = t.text_dim
    Palette.TEXT_FAINT = t.text_faint
    Palette.CONSOLE_FG = t.console_fg
    Palette.INPUT_ECHO = t.input_echo
    Palette.SYSTEM_MSG = t.system_msg
    Palette.SELECTION_FG = t.selection_fg
    Palette.BG_CARDHEAD = t.bg_cardhead
    Palette.CARDHEAD_FG = t.cardhead_fg
    Palette.CARDHEAD_SUB = t.cardhead_sub
    Palette.APPNAME_FG = t.appname_fg
    Palette.SCROLL_HOVER = t.scroll_hover
    ANSI_16[:] = list(t.ansi)   # keep the same list object; refresh contents
    DISPLAY_FONT = t.display_font
    BODY_FONT = t.body_font
    CONSOLE_FONT = t.console_font
    ACTIVE_THEME = t
    return t


apply_theme(DEFAULT_THEME_ID)   # populate Palette at import


def _rel_luma(hex_color: str) -> float:
    """WCAG relative luminance of a #rrggbb / #rgb color (0=black, 1=white).
    Pure-python so ui_theme stays Qt-free."""
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    lin = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _check_icon_path(fill_hex: str) -> str:
    """Write a checkmark SVG whose stroke contrasts with `fill_hex` (the checked
    indicator's fill) and return a forward-slashed file path, or "" on failure.

    Qt's stylesheet `image:` renders a file/qsvg url but NOT a `data:` URI
    (verified empirically), so the glyph has to live on disk. It's tiny and
    rewritten per theme. The stroke is black or white -- whichever CONTRASTS
    MORE with the fill (WCAG ratio; the crossover sits near luma 0.18, so most
    accents take the black tick) -- so the check stays legible on ANY skin's
    accent fill."""
    lum = _rel_luma(fill_hex)
    stroke = "#101010" if (lum + 0.05) / 0.05 >= 1.05 / (lum + 0.05) else "#ffffff"
    svg = ("<svg xmlns='http://www.w3.org/2000/svg' width='16' height='16' "
           "viewBox='0 0 16 16'><path d='M3.6 8.4 L6.5 11.4 L12.5 4.6' "
           f"fill='none' stroke='{stroke}' stroke-width='2.2' "
           "stroke-linecap='round' stroke-linejoin='round'/></svg>")
    try:
        d = os.path.join(tempfile.gettempdir(), "aihive-theme")
        os.makedirs(d, exist_ok=True)
        # name by the fill so switching skins never reads a stale color
        path = os.path.join(d, f"check-{fill_hex.lstrip('#')}.svg")
        with open(path, "w", encoding="utf-8") as f:
            f.write(svg)
        return path.replace("\\", "/")
    except OSError:
        return ""


def build_qss(chrome_family: str = "Segoe UI", console_px: int | None = None) -> str:
    p = Palette
    cpx = int(console_px if console_px else CONSOLE_FONT_PX)
    # checked-indicator glyph (written to disk; see _check_icon_path). Empty
    # string if the write failed -- the accent fill alone still reads as checked.
    _check_icon = _check_icon_path(p.ACCENT_GOLD)
    check_img = f'image: url("{_check_icon}");' if _check_icon else ""
    return f"""
* {{
    font-family: "{chrome_family}";
    font-size: 13px;
    color: {p.TEXT};
    outline: none;
}}
QMainWindow, #PageStack, #CentralBody {{ background: {p.BG_ROOT}; }}
QToolTip {{
    background: {p.BG_PANEL}; color: {p.TEXT};
    border: 1px solid {p.BORDER}; padding: 3px 6px;
}}

/* ---------------------------------------------------------- top bar --- */
#TopBar {{ background: {p.BG_PANEL}; border-bottom: 1px solid {p.ACCENT_GOLD_DIM}; }}
#Logo {{ color: {p.ACCENT_GOLD}; font-size: 17px; font-weight: 700; }}
#AppName {{ font-family: {DISPLAY_FONT}; font-size: 16px; font-weight: 700;
            letter-spacing: 0.5px; color: {p.APPNAME_FG}; }}
#VersionBadge {{
    background: {p.BG_HOVER}; color: {p.TEXT_DIM};
    border: 1px solid {p.BORDER}; border-radius: 3px;
    padding: 1px 6px; font-size: 11px;
}}
#Breadcrumb {{ color: {p.TEXT_DIM}; }}
#ThemeSelect {{
    background: {p.BG_INPUT}; border: 1px solid {p.BORDER}; border-radius: 3px;
    padding: 2px 8px; color: {p.TEXT}; font-size: 11px;
}}
#ThemeSelect:hover {{ border-color: {p.ACCENT_GOLD}; }}
#ThemeSelect QAbstractItemView {{
    background: {p.BG_PANEL}; border: 1px solid {p.BORDER};
    selection-background-color: {p.BG_ACTIVE}; color: {p.TEXT};
}}

/* ---------------------------------------------------------- sidebar --- */
#Sidebar {{ background: {p.BG_PANEL}; border-right: 1px solid {p.BORDER}; }}
#SidebarTitle {{
    color: {p.ACCENT_GOLD}; font-family: {DISPLAY_FONT}; font-size: 12px;
    letter-spacing: 2px; font-weight: 700;
}}
#WsCount {{ color: {p.TEXT_FAINT}; font-size: 11px; font-weight: 700; }}
#WsList {{ background: transparent; border: none; }}
#WsList::item {{ background: transparent; border: none; padding: 0; }}
#WsList::item:selected {{ background: transparent; }}
#WsList::item:hover {{ background: transparent; }}

WorkspaceRow {{
    background: transparent;
    border: none;
    border-left: 2px solid transparent;
}}
WorkspaceRow:hover {{ background: {p.BG_HOVER}; }}
/* The SELECTED workspace owns the theme accent: a thick bright bar and an
   accent-colored, bolder name. Nothing else in the sidebar wears the accent
   as a fill (category groups are neutral, see sidebar.drawRow), so the
   current workspace is unmistakable even when other rows are grouped. */
WorkspaceRow[active="true"] {{
    background: {p.BG_ACTIVE};
    border-left: 4px solid {p.ACCENT_GOLD};
}}
WorkspaceRow[active="true"] #WsName {{ color: {p.ACCENT_GOLD}; font-weight: 700; }}
WorkspaceRow[search_hit="true"] {{
    background: rgba(217,178,74,0.20);
    border-left: 3px solid {p.ACCENT_GOLD};
}}
#WsName {{ font-family: {BODY_FONT}; font-size: 16px; font-weight: 600; }}
#WsRenameEdit {{
    background: {p.BG_INPUT}; border: 1px solid {p.ACCENT_BLUE};
    border-radius: 2px; padding: 1px 4px; font-size: 16px;
}}
#WsFolderBtn {{
    background: transparent; border: 1px solid transparent; border-radius: 3px;
    padding: 2px 6px; color: {p.TEXT}; font-size: 17px; font-weight: 900;
}}
#WsFolderBtn:hover {{ background: {p.BG_HOVER}; color: {p.ACCENT_BLUE};
                     border-color: {p.BORDER}; }}
#WsTreeBtn {{
    background: transparent; border: 1px solid transparent; border-radius: 3px;
    padding: 2px 4px; color: {p.TEXT}; font-size: 15px; font-weight: 900;
}}
#WsTreeBtn:hover {{ background: {p.BG_HOVER}; color: {p.ACCENT_GOLD};
                   border-color: {p.BORDER}; }}
#WsList::branch {{ background: transparent; border: none; }}
#WsQ {{
    background: rgba(217,178,74,0.18); color: {p.YELLOW};
    border: 1px solid {p.YELLOW}; border-radius: 9px;
    font-weight: 800; font-size: 12px; padding: 0; min-width: 18px;
    max-width: 18px; min-height: 18px; max-height: 18px;
}}
#WsQ:hover {{ background: rgba(217,178,74,0.34); }}

/* sidebar search: the magnifier toggle + the overlay input it reveals */
#SearchBtn {{
    background: transparent; border: 1px solid transparent; border-radius: 3px;
    padding: 1px 4px; color: {p.TEXT_DIM}; font-size: 13px;
}}
#SearchBtn:hover {{ background: {p.BG_HOVER}; color: {p.ACCENT_GOLD};
                   border-color: {p.BORDER}; }}
#SidebarSearch {{
    background: {p.BG_INPUT}; border: 1px solid {p.ACCENT_GOLD};
    border-radius: 3px; padding: 1px 6px; color: {p.TEXT};
    font-family: {BODY_FONT}; font-size: 13px;
}}

/* category headers (collapsible workspace groups) */
#AddCatBtn {{
    background: transparent; border: 1px solid transparent; border-radius: 3px;
    padding: 1px 4px; color: {p.ACCENT_GOLD}; font-size: 13px;
}}
#AddCatBtn:hover {{ background: {p.BG_HOVER}; border-color: {p.BORDER}; }}
/* the primary "add workspace" action: a bold, evident gold "+" in a pill */
#AddWsBtn {{
    color: {p.ACCENT_GOLD}; font-size: 18px; font-weight: 800;
    background: {p.BG_ACTIVE}; border: 1px solid {p.BORDER};
    border-radius: 4px; padding: 0 8px; min-width: 16px;
}}
#AddWsBtn:hover {{
    background: {p.BG_HOVER}; border-color: {p.ACCENT_GOLD};
    color: {p.ACCENT_GOLD};
}}
#WsCategory {{
    background: transparent; border: none;
    border-top: 1px solid {p.BORDER};
}}
#WsCategory:hover {{ background: {p.BG_HOVER}; }}
#CatCaret {{
    background: transparent; border: none; color: {p.TEXT_DIM};
    font-size: 11px; padding: 0 2px;
}}
#CatCaret:hover {{ color: {p.ACCENT_GOLD}; }}
#CatName {{
    font-family: {DISPLAY_FONT}; font-size: 15px; letter-spacing: 1px;
    font-weight: 700; color: {p.ACCENT_GOLD};
}}
#CatCount {{
    color: {p.TEXT_FAINT}; font-size: 10px; font-weight: 700;
    background: {p.BG_ACTIVE}; border-radius: 7px; padding: 0 5px;
}}

/* inline agent rows (a workspace expanded to list its agents, folder-tree
   style): the name sits at the left with its task summary beside it */
#WsAgentRow {{
    background: transparent; border-left: 2px solid transparent;
}}
#WsAgentRow:hover {{ background: {p.BG_HOVER}; }}
#WsAgentRow[waiting="true"] {{
    background: rgba(217,178,74,0.12); border-left: 2px solid {p.YELLOW};
}}
#WsAgentRow[search_hit="true"] {{
    background: rgba(217,178,74,0.22); border-left: 2px solid {p.ACCENT_GOLD};
}}
#WsAgentDot {{ font-size: 10px; }}
#WsAgentName {{ font-family: {BODY_FONT}; font-size: 13px; font-weight: 600; }}
#WsAgentTask {{ color: {p.TEXT_DIM}; font-size: 13px; }}
#WsAgentQ {{ color: {p.YELLOW}; font-size: 14px; font-weight: 800; }}
/* cut off by the usage limit: amber like the "?", but deliberately NOT the
   error red: the agent is fine, it is only waiting for the window to reopen */
#WsAgentLimit {{ color: {p.ACCENT_GOLD}; font-size: 13px; }}

/* inline file explorer rows (a workspace expanded to its VS Code-style file
   tree): a type/folder icon + the name; the revealed row (a file jumped to from
   a conversation Ctrl+click) gets a gold-tinted highlight */
#WsTreeRow {{
    background: transparent; border-left: 2px solid transparent;
}}
#WsTreeRow:hover {{ background: {p.BG_HOVER}; }}
#WsTreeRow[revealed="true"] {{
    background: rgba(217,178,74,0.20); border-left: 2px solid {p.ACCENT_GOLD};
}}
#WsTreeCaret {{ color: {p.TEXT_DIM}; font-size: 10px; }}
#WsTreeIcon {{ font-size: 11px; }}
#WsTreeName {{ font-family: {BODY_FONT}; font-size: 12px; color: {p.TEXT}; }}
#WsTreeRow[dir="true"] #WsTreeName {{ font-weight: 600; }}
#WsTreeMore {{ color: {p.TEXT_FAINT}; font-size: 11px; font-style: italic;
              padding-left: 24px; }}

/* ----------------------------------------------------- terminal card --- */
TerminalCard {{
    background: {p.BG_CARD};
    border: 1px solid {p.BORDER};
    border-radius: 4px;
}}
TerminalCard[focused="true"] {{ border: 1px solid {p.ACCENT_ORANGE}; }}
#CardHeader {{
    background: {p.BG_CARDHEAD};
    border-bottom: 1px solid {p.ACCENT_GOLD_DIM};
    border-top-left-radius: 4px; border-top-right-radius: 4px;
}}
#StatusGlyph {{ color: {p.CARDHEAD_SUB}; font-size: 11px; }}
#StatusGlyph[state="starting"] {{ color: {p.ACCENT_ORANGE}; }}
#StatusGlyph[state="running"] {{ color: {p.GREEN}; }}
#StatusGlyph[state="dead"] {{ color: {p.RED}; }}
#CardTitle {{ font-family: {DISPLAY_FONT}; font-size: 13px; font-weight: 700;
              color: {p.CARDHEAD_FG}; }}
#CardTitleEdit {{
    background: {p.BG_INPUT}; border: 1px solid {p.ACCENT_BLUE};
    border-radius: 2px; padding: 0px 4px;
    font-family: {DISPLAY_FONT}; font-size: 13px; font-weight: 700;
    color: {p.CARDHEAD_FG};
}}
#CardRole {{ color: {p.CARDHEAD_SUB}; font-size: 11px; }}
/* live model + effort ("Opus 5 . high"), a shade brighter than the role so it
   reads as state rather than another label */
#CardModel {{ color: {p.ACCENT_GOLD_DIM}; font-size: 11px; font-weight: 700; }}
#CardTaskSummary {{ color: {p.CARDHEAD_SUB}; font-size: 13px; font-style: italic; }}
#CardTokens {{
    color: {p.CARDHEAD_SUB}; font-size: 10px; font-weight: 700;
    padding: 0px 4px;
}}
#CardLimitMark {{ color: {p.ACCENT_GOLD}; font-size: 13px; }}
#Console {{
    background: {p.BG_CONSOLE};
    border: none;
    font-family: {CONSOLE_FONT};
    font-size: {cpx}px;
    color: {p.CONSOLE_FG};
    selection-background-color: {p.SELECTION};
    selection-color: {p.SELECTION_FG};
    padding: 4px;
}}
#CardInput {{
    background: {p.BG_INPUT};
    border: none; border-top: 1px solid {p.BORDER};
    border-bottom-left-radius: 4px; border-bottom-right-radius: 4px;
    font-family: {CONSOLE_FONT};
    font-size: {cpx}px;
    padding: 5px 8px;
    color: {p.CONSOLE_FG};
    placeholder-text-color: {p.TEXT_FAINT};
}}
#CardInput:focus {{ border-top: 1px solid {p.ACCENT_ORANGE}; }}
#EmptyState {{ color: {p.TEXT_FAINT}; font-size: 14px; }}

/* ----------------------------------------------------------- buttons --- */
QToolButton {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 3px;
    padding: 3px 8px;
    color: {p.TEXT_DIM};
}}
QToolButton:hover {{ background: {p.BG_HOVER}; border-color: {p.BORDER}; color: {p.TEXT}; }}
QToolButton:pressed {{ background: {p.BG_ACTIVE}; }}
QToolButton:disabled {{ color: {p.TEXT_FAINT}; }}
/* compact single-glyph buttons live in the ultramarine running-head, so they
   take the head's subtitle color, not the body text color */
#CardClose, #WsDelete,
#CardFontDec, #CardFontInc, #CardMaximize {{
    padding: 3px 6px; font-size: 14px; color: {p.CARDHEAD_SUB};
}}
#CardFontDec:hover,
#CardFontInc:hover, #CardMaximize:hover {{ color: {p.CARDHEAD_FG}; }}
/* the A-/A+ font steppers read better a hair smaller than the glyph icons;
   the maximize/restore glyph a hair larger (later rules win on equal id
   specificity) */
#CardFontDec, #CardFontInc {{ font-size: 12px; font-weight: 700; }}
#CardMaximize {{ font-size: 16px; }}
#WsDelete {{ color: {p.TEXT}; font-size: 17px; font-weight: 900; }}
#GlobalFontBtn {{
    background: transparent; border: 1px solid {p.BORDER}; border-radius: 3px;
    padding: 2px 7px; color: {p.TEXT_DIM}; font-weight: 700;
}}
#GlobalFontBtn:hover {{ border-color: {p.ACCENT_BLUE}; color: {p.TEXT}; }}
#SoundToggle {{
    background: transparent; border: 1px solid {p.BORDER}; border-radius: 3px;
    padding: 2px 6px; color: {p.TEXT}; font-size: 14px;
}}
#SoundToggle:hover {{ border-color: {p.ACCENT_GOLD}; }}
/* caption naming the two auto-recovery switches below, so the LED pair
   doesn't read as unlabeled decoration */
#RecoveryLabel {{ color: {p.TEXT_DIM}; font-size: 12px; }}
/* the two auto-recovery switches beside the plan-usage readout. Armed reads
   as lit (accent border + full-strength glyph + green LED), off reads as
   faint with a dark LED, so "will my work resume by itself?" is answerable
   without hovering. */
#RecoveryToggle {{
    background: transparent; border: 1px solid {p.BORDER}; border-radius: 3px;
    padding: 2px 6px; color: {p.TEXT_FAINT}; font-size: 14px;
}}
#RecoveryToggle:checked {{
    color: {p.ACCENT_GOLD}; border-color: {p.ACCENT_GOLD};
}}
#RecoveryToggle:hover {{
    background: {p.BG_HOVER}; border-color: {p.ACCENT_BLUE}; color: {p.TEXT};
}}
#CardClose:hover, #WsDelete:hover {{ color: {p.RED}; border-color: {p.RED}; }}
QPushButton {{
    background: {p.BG_HOVER}; border: 1px solid {p.BORDER};
    border-radius: 3px; padding: 5px 14px;
}}
QPushButton:hover {{ border-color: {p.ACCENT_BLUE}; }}
QPushButton:default {{ border-color: {p.ACCENT_BLUE}; }}
QPushButton:disabled {{ color: {p.TEXT_FAINT}; }}

/* ------------------------------------------------- inputs & dialogs --- */
QLineEdit {{
    background: {p.BG_INPUT}; border: 1px solid {p.BORDER};
    border-radius: 3px; padding: 4px 8px;
}}
QLineEdit:focus {{ border-color: {p.ACCENT_BLUE}; }}
QLineEdit:disabled {{ color: {p.TEXT_FAINT}; }}
QComboBox {{
    background: {p.BG_INPUT}; border: 1px solid {p.BORDER};
    border-radius: 3px; padding: 4px 8px;
}}
QComboBox:focus {{ border-color: {p.ACCENT_BLUE}; }}
QComboBox QAbstractItemView {{
    background: {p.BG_PANEL}; border: 1px solid {p.BORDER};
    selection-background-color: {p.BG_ACTIVE};
}}
QDialog, QMessageBox, QFileDialog {{ background: {p.BG_PANEL}; }}
QLabel {{ background: transparent; }}
/* Checkboxes: the platform default draws a near-invisible tick. Give it a
   clear box (empty = input surface, checked = accent-filled) and a
   contrast-picked check glyph (black on light accents, white on dark) so the
   tick is obvious on every skin. */
QCheckBox {{ background: transparent; spacing: 7px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border-radius: 3px;
    border: 1px solid {p.BORDER}; background: {p.BG_INPUT};
}}
QCheckBox::indicator:hover {{ border-color: {p.ACCENT_GOLD}; }}
QCheckBox::indicator:checked {{
    background: {p.ACCENT_GOLD}; border: 1px solid {p.ACCENT_GOLD};
    {check_img}
}}
QCheckBox::indicator:checked:hover {{ border-color: {p.ACCENT_GOLD}; }}
QCheckBox::indicator:disabled {{
    background: {p.BG_HOVER}; border-color: {p.BORDER};
}}
QCheckBox:disabled {{ color: {p.TEXT_FAINT}; }}

/* -------------------------------------------------------- scrollbars --- */
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {p.BORDER}; min-height: 24px; border-radius: 5px;
}}
QScrollBar::handle:vertical:hover {{ background: {p.SCROLL_HOVER}; }}
QScrollBar:horizontal {{
    background: transparent; height: 10px; margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {p.BORDER}; min-width: 24px; border-radius: 5px;
}}
QScrollBar::handle:horizontal:hover {{ background: {p.SCROLL_HOVER}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QScrollArea {{ background: transparent; border: none; }}
#GridHost {{ background: {p.BG_ROOT}; }}

/* ------------------------------------------------ workspace header (v2) --- */
#WorkspaceHeader {{
    background: {p.BG_PANEL}; border-bottom: 1px solid {p.BORDER};
}}
#HeaderFolderIcon {{ color: {p.TEXT_DIM}; font-size: 13px; }}
#HeaderPath {{ color: {p.TEXT_DIM}; font-size: 12px; }}
#GridButton, #ActivityToggle {{
    background: {p.BG_HOVER}; border: 1px solid {p.BORDER};
    border-radius: 3px; padding: 3px 8px; color: {p.TEXT};
}}
#GridButton:hover, #ActivityToggle:hover {{ border-color: {p.ACCENT_BLUE}; }}
#ActivityToggle:checked {{
    border-color: {p.ACCENT_BLUE}; color: {p.ACCENT_BLUE};
}}
/* Agent/File Map line-type filters: colour-coded to their edge, dimmed when
   toggled off so "showing this kind" reads at a glance */
#MapWriteToggle, #MapReadToggle {{
    background: {p.BG_HOVER}; border: 1px solid {p.BORDER};
    border-radius: 3px; padding: 3px 8px; color: {p.TEXT_FAINT};
}}
#MapWriteToggle:hover, #MapReadToggle:hover {{ border-color: {p.ACCENT_BLUE}; }}
#MapWriteToggle:checked {{
    border-color: {p.ACCENT_GOLD}; color: {p.ACCENT_GOLD};
}}
#MapReadToggle:checked {{ border-color: {p.TEXT_DIM}; color: {p.TEXT}; }}
#GridSelectorPopup {{
    background: {p.BG_PANEL}; border: 1px solid {p.BORDER}; border-radius: 6px;
}}
#GridSelectorPopup QToolButton {{
    background: {p.BG_CARD}; border: 1px solid {p.BORDER}; border-radius: 4px;
    color: {p.TEXT_DIM}; font-size: 10px; padding-top: 26px;
}}
#GridSelectorPopup QToolButton:hover {{ border-color: {p.ACCENT_BLUE}; }}
#GridSelectorPopup QToolButton:checked {{
    border-color: {p.ACCENT_BLUE}; color: {p.TEXT};
}}
#ProviderNote {{ color: {p.TEXT_DIM}; font-size: 11px; }}

/* -------------------------------------------------- activity panel (v2) --- */
#ActivityPanel {{
    background: {p.BG_PANEL}; border-left: 1px solid {p.BORDER};
}}
#ActivityTitle {{
    color: {p.ACCENT_GOLD}; font-family: {DISPLAY_FONT};
    font-size: 12px; font-weight: 700;
    letter-spacing: 1.5px; padding: 11px 10px 7px 12px;
    border-bottom: 1px solid {p.ACCENT_GOLD_DIM};
}}
#ActivityScroll {{ background: transparent; border: none; }}
#ActivitySection {{
    color: {p.TEXT_DIM}; font-size: 11px; font-weight: 700;
    margin-top: 6px;
}}
#RosterItem {{
    background: {p.BG_CARD}; border: 1px solid {p.BORDER}; border-radius: 4px;
}}
#RosterHead {{ font-size: 12px; }}
#RosterTask {{
    background: {p.BG_INPUT}; border: 1px solid {p.BORDER}; border-radius: 3px;
    padding: 2px 6px; font-size: 11px; color: {p.CONSOLE_FG};
}}
#RosterTask:focus {{ border-color: {p.ACCENT_BLUE}; }}
#ActivityLog, #ActivityFiles {{
    font-family: {CONSOLE_FONT}; font-size: 11px; color: {p.TEXT_DIM};
}}

/* -------------------------------------------------- empty grid slot (v2) --- */
#EmptySlot {{
    background: {p.BG_CARD}; border: 1px dashed {p.BORDER}; border-radius: 4px;
}}
#EmptySlot:hover {{ border: 1px dashed {p.ACCENT_BLUE}; background: {p.BG_HOVER}; }}
#EmptySlotPlus {{ color: {p.TEXT_FAINT}; font-size: 30px; }}
#EmptySlotLabel {{ color: {p.TEXT_FAINT}; font-size: 12px; }}
#EmptySlot:hover #EmptySlotPlus, #EmptySlot:hover #EmptySlotLabel {{
    color: {p.ACCENT_BLUE};
}}
"""


def repolish(widget) -> None:
    """Force QSS re-evaluation after a dynamic property change."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()
