"""Terminal fonts the user loads from a file (Options > Terminal font >
Load a custom font...).

`install` checks the picked file, COPIES it into the fonts folder beside
session.json and offers it in the picker, so moving or deleting the original
breaks nothing. Every launch registers that folder again with `load_dir`
before the session's font id is read (main.setup_application and
MainWindow.__init__), which is what lets a saved custom pick resolve.

The picker offers one entry per family. Another file of a family it already
offers (the Bold of a Regular loaded earlier) goes into a `styles` folder
inside the fonts folder: `load_dir` registers it but never offers it, so Qt
draws bold with the real face while the saved pick keeps its id.

Qt's addApplicationFont reads TrueType, OpenType and TrueType collection
files, and those three are all we accept. The check reads the first four
bytes as well as the name, because a JPEG renamed to .ttf has the right name
and Qt's own failure says nothing the user can act on.
"""

from __future__ import annotations

import filecmp
import os
import shutil

from . import ui_theme

FONT_EXTS = (".ttf", ".otf", ".ttc")
MAX_FONT_BYTES = 50 * 1024 * 1024
# the sfnt version tag a font file starts with: TrueType outlines (Windows
# and Apple spellings), CFF outlines, and a collection's header
_FONT_MAGIC = (b"\x00\x01\x00\x00", b"true", b"OTTO", b"ttcf")
_WEB_MAGIC = (b"wOFF", b"wOF2")

# normalized path -> (Qt's application font id, its families), this run
_registered: dict[str, tuple[int, list[str]]] = {}


def fonts_dir(store_dir) -> str:
    """Where custom font copies live: beside session.json."""
    return os.path.join(str(store_dir), "fonts")


def styles_dir(folder: str) -> str:
    """Where the other files of an offered family live, inside `folder`."""
    return os.path.join(folder, "styles")


def _font_files(folder: str) -> list[str]:
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    return [os.path.join(folder, n) for n in names
            if n.lower().endswith(FONT_EXTS)
            and os.path.isfile(os.path.join(folder, n))]


def validate(path: str) -> str | None:
    """Check a file the user picked. Returns None if it looks like a font,
    else a short reason to show them (no em dash, the user reads it)."""
    name = os.path.basename(path)
    ext = os.path.splitext(path)[1].lower()
    if ext not in FONT_EXTS:
        return (f'"{name}" is not a font file. AI Hive can load TrueType '
                "(.ttf), OpenType (.otf) and TrueType collection (.ttc) "
                "fonts.")
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(4)
    except OSError:
        return "The file could not be read."
    if size == 0:
        return "The file is empty."
    if size > MAX_FONT_BYTES:
        return (f"The file is {size / 1024 / 1024:.1f} MB. A font can be at "
                f"most {MAX_FONT_BYTES // (1024 * 1024)} MB.")
    if head in _WEB_MAGIC:
        return (f'"{name}" is a web font (WOFF) with a {ext} name. Load the '
                "font's TTF or OTF file instead.")
    if head not in _FONT_MAGIC:
        return f'"{name}" is not a font file, even though its name ends in {ext}.'
    return None


def _key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def register(path: str) -> list[str]:
    """Register a font file with Qt for this run and return its families,
    [] when Qt can't read one. A path registered before returns its first
    result without registering it twice."""
    key = _key(path)
    if key in _registered:
        return list(_registered[key][1])
    from PySide6.QtGui import QFontDatabase
    qt_id = QFontDatabase.addApplicationFont(path)
    families = list(QFontDatabase.applicationFontFamilies(qt_id)) \
        if qt_id >= 0 else []
    if families:
        _registered[key] = (qt_id, families)
    elif qt_id >= 0:
        QFontDatabase.removeApplicationFont(qt_id)
    return families


def _drop(path: str) -> None:
    """Unregister a copy and delete it."""
    entry = _registered.pop(_key(path), None)
    if entry is not None:
        from PySide6.QtGui import QFontDatabase
        QFontDatabase.removeApplicationFont(entry[0])
    try:
        os.remove(path)
    except OSError:
        pass   # already gone: nothing to clean up


def _offer(file_name: str, families: list[str]) -> str:
    """Put a registered copy in the picker and return its font id."""
    fid = ui_theme.CUSTOM_FONT_PREFIX + file_name
    ui_theme.CUSTOM_TERMINAL_FONTS[fid] = (families[0], (families[0],))
    return fid


def _offered_id(family: str) -> str | None:
    """The picker entry that already draws `family`, if any. "theme" has no
    families of its own and never matches."""
    for fid, (_label, fams) in ui_theme.terminal_fonts().items():
        if fams and fams[0] == family:
            return fid
    return None


def load_dir(folder: str) -> None:
    """Register every font file in `folder` and offer it in the picker, and
    register the files in its styles folder without offering them. A file
    Qt can't read is skipped, so a damaged copy costs only itself."""
    for path in _font_files(folder):
        families = register(path)
        if families:
            _offer(os.path.basename(path), families)
    for path in _font_files(styles_dir(folder)):
        register(path)


def _copy_into(source: str, folder: str) -> str:
    """Copy `source` into `folder` under a free name; returns the copy."""
    base, ext = os.path.splitext(os.path.basename(source))
    ext = ext.lower()
    target = os.path.join(folder, base + ext)
    n = 2
    while os.path.exists(target):
        target = os.path.join(folder, f"{base}-{n}{ext}")
        n += 1
    os.makedirs(folder, exist_ok=True)
    shutil.copyfile(source, target)
    return target


def install(source: str, folder: str) -> tuple[str | None, str | None]:
    """Validate `source`, copy it into `folder`, register the copy and offer
    it. Returns (font id, None), or (None, the reason to show the user). A
    font whose family the picker already has returns that entry's id. A
    built-in family or a byte-identical copy keeps no file, so loading a
    file twice adds nothing. Any other file of a custom family is kept in
    the styles folder."""
    reason = validate(source)
    if reason is not None:
        return None, reason
    try:
        target = _copy_into(source, folder)
    except OSError as e:
        return None, f"The file could not be copied: {e.strerror or e}"
    families = register(target)
    if not families:
        _drop(target)
        return None, ("Windows could not read a font from this file. It may "
                      "be damaged.")
    known = _offered_id(families[0])
    if known is None:
        return _offer(os.path.basename(target), families), None
    _drop(target)
    if known not in ui_theme.CUSTOM_TERMINAL_FONTS or any(
            filecmp.cmp(source, path, shallow=False)
            for path in _font_files(folder)
            + _font_files(styles_dir(folder))):
        return known, None
    try:
        register(_copy_into(source, styles_dir(folder)))
    except OSError as e:
        return None, f"The file could not be copied: {e.strerror or e}"
    return known, None


def forget(font_id: str, folder: str) -> None:
    """Take a custom font out of the picker, unregister it and delete its
    copy and its family's style files. A built-in id is left alone."""
    entry = ui_theme.CUSTOM_TERMINAL_FONTS.pop(font_id, None)
    if entry is None:
        return
    _drop(os.path.join(folder, font_id[len(ui_theme.CUSTOM_FONT_PREFIX):]))
    for path in _font_files(styles_dir(folder)):
        if register(path)[:1] == [entry[1][0]]:
            _drop(path)
    try:
        os.rmdir(styles_dir(folder))
    except OSError:
        pass   # missing, or another family's styles are still in it
