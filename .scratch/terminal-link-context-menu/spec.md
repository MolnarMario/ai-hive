# Right-click a file path in the terminal to copy it or reveal its folder

Status: ready-for-agent
Area: `app/widgets/terminal_view.py`, plus one README paragraph, one smoke
check, one version bump.

## What the user asked for

Ctrl+click on a path or URL in an agent's output already opens it. The ask is
the other half: right-click that same path and get a menu with "copy the file
path", so the folder it lives in is reachable without hunting for it.

## The short version

`TerminalView.contextMenuEvent` already builds a QMenu (Copy / Paste /
Select all). When the pointer is over a token that `_link_at()` classifies,
prepend the four actions the Agent/File Map menu already uses, and separate
them from the clipboard items. Nothing else changes.

## What already exists (do not rebuild any of this)

Everything the feature needs is in the tree today.

`app/fsopen.py` has the three shell helpers, already shipping and already used
by the file map:

- `open_path(path)` - default program, with the `os.startfile` fallback
- `open_with(path)` - the Windows "Open with..." picker
- `reveal_in_folder(path)` - `explorer /select,<path>`, i.e. the folder opens
  with the file already highlighted

`app/widgets/agent_file_map.py:485-495` is the precedent, verbatim:

```python
menu.addAction("Open", lambda: fsopen.open_path(path))
menu.addAction("Open with...", lambda: fsopen.open_with(path))
menu.addAction("Reveal in folder", lambda: fsopen.reveal_in_folder(path))
menu.addAction("Copy path", lambda: QApplication.clipboard().setText(path))
```

Match those four labels exactly. Two menus that do the same thing to a file
should read the same, and the file map's wording is the one already on screen.

`TerminalView._link_at(row, col)` (terminal_view.py:1667) returns
`("url", value)` or `("file", abspath)` for the token under a cell, or `None`.
It is the same call `mousePressEvent` makes on Ctrl+click, so the right-click
menu and Ctrl+click can never disagree about what counts as a link. It already
handles:

- absolute paths, and relative ones resolved against `set_base_dir()` (the
  agent's cwd), which is how `app/widgets/sidebar.py` is clickable
- a trailing `:line[:col]` suffix, stripped before the existence check
- wrapping quotes and brackets
- scrollback rows, via `_visible_line(row, hist, off)` - a right-click on a
  scrolled-back line resolves against the history line, not the live screen

`_cell_at(pos)` (terminal_view.py:1277) maps a widget-space point to
`(row, col)` and clamps to the screen. A `QContextMenuEvent` has
`.pos()`/`.globalPos()` carrying the `.x()`/`.y()` that `_cell_at` reads, so it
can be passed straight in.

Right-click is safe to build on: `mousePressEvent` branches on `LeftButton` and
`MiddleButton` only, so a right press moves no caret, sends nothing to the
child, and leaves any selection alone. `contextMenuEvent` is reachable in every
mode, including while the child has mouse tracking on.

## The change

### 1. Split the menu build out of `contextMenuEvent`

`contextMenuEvent` (terminal_view.py:2166) calls `menu.exec()`, which blocks -
so the headless suite can never drive it. Move the construction into a helper
that returns the menu without showing it, and have `contextMenuEvent` position
and exec it. This is the only structural change, and it is what makes the
feature testable.

```python
def _build_context_menu(self, row: int, col: int) -> QMenu:
    """Assemble the terminal's right-click menu for the cell under the
    pointer. Returns it UNSHOWN so a test can read the actions and trigger
    one without a blocking exec()."""
    from .. import fsopen

    menu = QMenu(self)
    target = self._link_at(row, col)
    if target:
        kind, value = target
        if kind == "file":
            menu.addAction("Open", lambda: fsopen.open_path(value))
            menu.addAction("Open with...", lambda: fsopen.open_with(value))
            menu.addAction("Reveal in folder",
                           lambda: fsopen.reveal_in_folder(value))
            menu.addAction("Copy path",
                           lambda: QGuiApplication.clipboard().setText(value))
        else:
            menu.addAction("Open link", lambda: fsopen.open_url(value))
            menu.addAction("Copy link address",
                           lambda: QGuiApplication.clipboard().setText(value))
        menu.addSeparator()
    # ... the existing Copy / Paste / separator / Select all block, unchanged
    return menu

def contextMenuEvent(self, event):
    menu = self._build_context_menu(*self._cell_at(event.pos()))
    menu.exec(event.globalPos())
```

Keep the existing Copy/Paste/Select all wiring as it is. The current code
compares `menu.exec()`'s return value against the three action objects, so
either return those actions alongside the menu, or convert them to lambdas the
way the link items are. Converting is cleaner; do whichever leaves the smaller
diff, and do not change what those three items do.

`fsopen` is imported lazily inside `_open_target` today (`from .. import
fsopen`); do the same here rather than adding a module-level import, so the
existing import shape is preserved. `QGuiApplication` is already imported in
this module.

### 2. Decisions already made, so the implementor does not re-litigate them

- **The link items only appear when there is a link under the pointer.** No
  greyed-out placeholders. Their presence is also the confirmation that the
  token was recognised as a real file, which a disabled row would not give.
- **Copy the RESOLVED absolute path**, which is what `_link_at` already
  returns - not the token as printed. A repo-relative `app/widgets/sidebar.py`
  pastes into Explorer's address bar only as an absolute path, and the `:42:7`
  suffix is already stripped. This is the whole point of the feature.
- **"Reveal in folder" is the direct answer to "get to the folder"**, since
  `explorer /select` opens the folder with the file highlighted. Copy path is
  the fallback for pasting elsewhere. Ship both.
- **URLs get two items, not four.** `_link_at` classifies URLs too, so a menu
  that offered nothing for them would look broken. Open and copy is all a URL
  has.
- Do not touch the selection or the caret on right-click. Right-click already
  does neither, and it must stay that way - the same class of bug as the
  menu-corruption fix in `_reposition_cursor`.
- No `waiting_probe()` gate is needed. That guard exists because
  `_reposition_cursor` sends synthesized arrow keys into the child; this menu
  sends the child nothing at all.

### 3. Test

Add one check function to `tests/smoke_test.py`, next to
`test_terminal_relative_link` (line 1938) - reuse its setup, which already
builds a temp dir, a real file, and a `TerminalView` with `set_base_dir`.

```python
def test_terminal_link_context_menu():
    """Right-clicking a path in the output offers Reveal in folder / Copy path,
    resolved to the ABSOLUTE path (Explorer needs an absolute path, and the
    token printed is usually repo-relative). Drives _build_context_menu
    directly: contextMenuEvent's exec() would block the headless suite."""
```

Assert, at minimum:

- over a path token, the action texts start with
  `["Open", "Open with...", "Reveal in folder", "Copy path"]`
- triggering "Copy path" puts `os.path.abspath(absf)` on the clipboard
  (verified in this venv: the offscreen QPA clipboard round-trips text)
- over a plain word, the menu carries none of those actions and still has
  Copy / Paste / Select all
- over a URL token, "Copy link address" copies the URL

Trigger **only** the copy actions. Triggering Open, Open with, or Reveal would
launch a real program from the suite.

Non-ASCII is banned in check names (cp1252 console), and the em-dash smoke
check parses every non-docstring string literal - so the menu labels must use
`...`, never an em dash. `"Open with..."` with three periods is what the file
map uses; keep it byte-identical.

### 4. Docs and version

- `README.md:321-327` (the "Conversation <-> files bridge" bullet) and
  `README.md:660-665` (the terminal mouse paragraph): add one clause saying a
  right-click on a link offers Reveal in folder and Copy path.
- `README.md:706` and `README.md:828` carry the suite's check count (1835
  today). Bump both by however many checks you added.
- Bump `__version__` in `app/__init__.py` (0.18.4 today). Every merge to `main`
  bumps it; this is a feature, so 0.19.0.
- `terminal_view.py`'s module docstring documents the Ctrl+click link behaviour
  at lines 91-105. Add a sentence there for the right-click menu - that
  docstring is the documentation of record for this file.

## Out of scope

- A "Copy relative path" variant. One copy item, and the absolute one is the
  useful one.
- Making the right-click menu context-aware for anything other than links.
- Any change to the underline scan, `_rescan_links`, or hover behaviour. The
  menu calls `_link_at` per right-click, which is one `os.path.exists` on a
  user-initiated gesture - no caching needed, and no reason to route it
  through the cached spans.

## Verify

```powershell
.venv\Scripts\python.exe tests\smoke_test.py
```

Pre-existing failures on this branch are environmental (the schedule-composer
countdown flake, and the live-Claude e2e checks). Compare against clean HEAD
before blaming the change.
