"""File-type icons — a single source of truth shared by the Agent/File Map and
the sidebar's inline file explorer.

A small emoji before a filename signals its kind at a glance. Emoji render in
colour via the platform emoji font (Segoe UI Emoji on Windows) and degrade to a
monochrome glyph in the pen colour elsewhere. Extensions map to a coarse
category; anything unknown gets the generic document icon.

Qt-free on purpose (only `os`): both a Qt widget and a headless test can import
it, and it carries no rendering concerns — just the name->glyph mapping.
"""

import os

# The font that actually renders these glyphs in colour on Windows. Widgets that
# paint the icons pick this up; on other platforms an empty family lets Qt fall
# back to whatever emoji font it resolves.
EMOJI_FONT = "Segoe UI Emoji" if os.name == "nt" else ""

FOLDER_ICON = "\N{FILE FOLDER}"               # closed folder
FOLDER_OPEN_ICON = "\N{OPEN FILE FOLDER}"     # expanded folder
DEFAULT_ICON = "\N{PAGE FACING UP}"           # generic file

FILE_ICONS = {
    # images
    **dict.fromkeys((".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".webp",
                     ".ico", ".tif", ".tiff", ".heic"), "\N{FRAME WITH PICTURE}"),
    # python
    ".py": "\N{SNAKE}", ".pyi": "\N{SNAKE}", ".pyw": "\N{SNAKE}",
    # web markup / styles
    ".html": "\N{GLOBE WITH MERIDIANS}", ".htm": "\N{GLOBE WITH MERIDIANS}",
    **dict.fromkeys((".css", ".scss", ".sass", ".less"), "\N{ARTIST PALETTE}"),
    # data / config
    **dict.fromkeys((".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
                     ".xml", ".env", ".properties"), "\N{GEAR}"),
    # docs
    **dict.fromkeys((".md", ".markdown", ".rst"), "\N{MEMO}"),
    **dict.fromkeys((".txt", ".log", ".text"), "\N{PAGE FACING UP}"),
    ".pdf": "\N{CLOSED BOOK}", ".doc": "\N{BLUE BOOK}", ".docx": "\N{BLUE BOOK}",
    # tabular / database
    **dict.fromkeys((".csv", ".tsv", ".xls", ".xlsx"), "\N{BAR CHART}"),
    **dict.fromkeys((".db", ".sqlite", ".sqlite3", ".sql"), "\N{FILE CABINET}"),
    # shell / scripts
    **dict.fromkeys((".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd"),
                    "\N{DESKTOP COMPUTER}"),
    # archives
    **dict.fromkeys((".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".bz2", ".xz"),
                    "\N{COMPRESSION}"),
    # media
    **dict.fromkeys((".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"),
                    "\N{MULTIPLE MUSICAL NOTES}"),
    **dict.fromkeys((".mp4", ".mov", ".mkv", ".avi", ".webm", ".wmv"),
                    "\N{FILM FRAMES}"),
    # code (many languages share the scroll)
    **dict.fromkeys((".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".c", ".h",
                     ".cpp", ".cc", ".cxx", ".hpp", ".java", ".go", ".rs", ".rb",
                     ".php", ".cs", ".swift", ".kt", ".kts", ".lua", ".r",
                     ".dart", ".scala", ".pl", ".vue", ".svelte"),
                    "\N{SCROLL}"),
}


def file_icon(name: str) -> str:
    """Emoji for a filename's type, by extension (generic doc if unknown)."""
    _, ext = os.path.splitext(name)
    return FILE_ICONS.get(ext.lower(), DEFAULT_ICON)
