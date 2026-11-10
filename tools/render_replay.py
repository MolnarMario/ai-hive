"""Replay a Claude card's raw stream through TerminalView and say where its
scrollback picked up doubled rows.

Takes either a capture from tools/render_capture.py or a dump AI Hive saved
on a RENDER-DUP line (<session dir>/render_dumps/*.json, see
app/render_dump.py). A dump is fed at the sizes the card had, at the offsets
it had them, the way TerminalCard._replay_with_marks rebuilds a card.

    .venv\\Scripts\\python.exe tools\\render_replay.py out.json
    .venv\\Scripts\\python.exe tools\\render_replay.py out.json --rows 40
    .venv\\Scripts\\python.exe tools\\render_replay.py out.json --conhost

--rows N prints the last N rows of scrollback and the screen ("|" marks the
screen). --raw replays with Claude's take-back rules off, which shows what
the bytes do on their own. --conhost also writes the stream into a real
conhost of the same size (the emulator Windows Terminal shares) and compares
its screen after every frame with the card's own pyte screen: a difference
is an emulation gap of ours, not Claude's doing. docs/agents/terminal-rendering.md walks through
reading the output.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FRAME_END = "\x1b[?25h"


def load(path: str) -> dict:
    """{"sizes": [(offset, rows, cols)], "stream": str, "marks": {}}."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and "chunks" in data:            # a capture
        sys.path.insert(0, ROOT)
        from app.terminal_agent import REPAINT_DONE
        stream = "".join(d for _, d in data["chunks"])
        marks = dict(data.get("marks", {}))
        # REPAINT_DONE where AI Hive would have written it after its own
        # repaint (render_capture.py marks the spots), later marks moved on
        for name, at in sorted(marks.items(), key=lambda m: -m[1]):
            if name.startswith("repaint done"):
                stream = stream[:at] + REPAINT_DONE + stream[at:]
                marks = {k: v + len(REPAINT_DONE) if v > at else v
                         for k, v in marks.items()}
        return {"sizes": [(0, data["rows"], data["cols"])],
                "stream": stream, "marks": marks}
    if isinstance(data, dict) and "stream" in data:             # a dump
        sizes = [tuple(g) for g in data.get("geometry") or []]
        if not sizes:
            det = data.get("detected", {})
            sizes = [(0, det.get("rows", 40), det.get("cols", 120))]
        print(f"dump of {data.get('agent')} (claude "
              f"{data.get('claude_version')}), detected "
              f"{data.get('detected')}")
        return {"sizes": sizes, "stream": data["stream"], "marks": {}}
    raise SystemExit(f"{path}: neither a capture nor a render dump")


def replay(src: dict, raw: bool, show: int) -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, ROOT)
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])  # noqa: F841
    from app.widgets.terminal_view import TerminalView, _row_text

    stream = src["stream"]
    # by offset only: a resize storm keeps its sizes in the order they came
    sizes = sorted(src["sizes"], key=lambda s: s[0])
    _, rows, cols = sizes[0]
    view = TerminalView(rows=rows, cols=cols, claude_repaints=not raw)
    view.screen.resize(rows, cols)
    pos = 0
    seen = []
    view.duplicateRowsSeen.connect(lambda info: seen.append((pos, info)))
    cuts = sorted({off for off, _, _ in sizes[1:]}
                  | set(src["marks"].values()) | {len(stream)})
    by_mark = {v: k for k, v in src["marks"].items()}
    for cut in cuts:
        # feed in frame-sized pieces, as the live view meets them
        while pos < cut:
            end = stream.find(_FRAME_END, pos, cut)
            end = cut if end < 0 else end + len(_FRAME_END)
            view.feed(stream[pos:end])
            pos = end
        for off, r, c in sizes:
            if off == cut:
                view.project_size(r, c)
        if cut in by_mark:
            print(f"-- mark {by_mark[cut]!r} at {cut}: "
                  f"{len(view.screen.history.top)} rows of scrollback")
    scr = view.screen
    blank = scr.default_char
    hist = [_row_text(r, scr.columns, blank) for r in scr.history.top]
    screen = [_row_text(scr.buffer[y], scr.columns, blank)
              for y in range(scr.lines)]
    for at, info in seen:
        print(f"doubled rows at stream offset {at}: period {info['period']}, "
              f"run {info['run']}: {info['text'][:80]!r}")
    print(f"{len(seen)} sighting(s); {len(hist)} rows of scrollback, "
          f"screen {scr.lines}x{scr.columns}")
    if show:
        for t in hist[-show:]:
            print("  " + t)
        for t in screen:
            print("| " + t)
    return 1 if seen else 0


def conhost_child(stream_path: str, out_path: str) -> None:
    """Runs inside the pty: writes the stream to this console frame by
    frame and records conhost's visible rows and cursor after each."""
    import ctypes
    k = ctypes.windll.kernel32
    h = k.GetStdHandle(-11)
    k.SetConsoleOutputCP(65001)
    # VT on and LF without CR, as Claude's console has it (ConPTY passes
    # Claude's bare "\n" through only because of DISABLE_NEWLINE_AUTO_RETURN)
    k.SetConsoleMode(h, 0x1 | 0x2 | 0x4 | 0x8)

    class COORD(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

    class RECT(ctypes.Structure):
        _fields_ = [("L", ctypes.c_short), ("T", ctypes.c_short),
                    ("R", ctypes.c_short), ("B", ctypes.c_short)]

    class INFO(ctypes.Structure):
        _fields_ = [("size", COORD), ("cur", COORD), ("attr", ctypes.c_ushort),
                    ("win", RECT), ("max", COORD)]

    # newline="": text mode would turn Claude's bare CRs into LFs
    with open(stream_path, encoding="utf-8", newline="") as fh:
        stream = fh.read()
    written = ctypes.c_uint()
    frames = []
    pos = 0
    while pos < len(stream):
        end = stream.find(_FRAME_END, pos)
        end = len(stream) if end < 0 else end + len(_FRAME_END)
        data = stream[pos:end].encode("utf-8")
        k.WriteFile(h, data, len(data), ctypes.byref(written), None)
        pos = end
        info = INFO()
        k.GetConsoleScreenBufferInfo(h, ctypes.byref(info))
        width = info.win.R - info.win.L + 1
        buf = ctypes.create_unicode_buffer(width + 1)
        rows = []
        for y in range(info.win.T, info.win.B + 1):
            got = ctypes.c_uint()
            k.ReadConsoleOutputCharacterW(h, buf, width, COORD(info.win.L, y),
                                          ctypes.byref(got))
            rows.append(buf.value[:got.value].rstrip())
        frames.append({"pos": pos, "rows": rows,
                       "cur": [info.cur.Y - info.win.T, info.cur.X]})
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(frames, fh)


def compare_conhost(src: dict) -> int:
    import tempfile
    import threading

    import pyte
    from winpty import PtyProcess

    if len(src["sizes"]) > 1:
        print("--conhost: the stream changes size; comparing at the first")
    _, rows, cols = sorted(src["sizes"])[0]
    # conhost answers these queries itself; written back they would echo
    stream = src["stream"].replace("\x1b[1t", "").replace("\x1b[c", "")
    tmp = tempfile.mkdtemp(prefix="aihive-conhost-")
    stream_path = os.path.join(tmp, "stream.txt")
    out_path = os.path.join(tmp, "frames.json")
    with open(stream_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(stream)
    proc = PtyProcess.spawn([sys.executable, os.path.abspath(__file__),
                             "--conhost-child", stream_path, out_path],
                            dimensions=(rows, cols))

    def drain():
        try:
            while proc.read(65536):
                pass
        except Exception:
            pass

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    t.join(timeout=300)
    with open(out_path, encoding="utf-8") as fh:
        frames = json.load(fh)
    sys.path.insert(0, ROOT)
    from app.widgets.terminal_view import _PRIVATE_CSI_RE, _new_history_screen

    # the card's own screen, grapheme clusters and all (app/grapheme_cells.py),
    # fed what TerminalView.feed hands it: pyte draws the "u" of a keyboard
    # protocol reply such as ESC[<u, so the view strips those first
    screen = _new_history_screen(cols, rows)
    feed = pyte.Stream(screen).feed

    def cell_text(data: str) -> str:
        # conhost hands back a cluster that isn't one UTF-16 unit as U+FFFD,
        # once for both of its cells
        if len(data) == 1 and ord(data) <= 0xFFFF:
            return data
        return data and chr(0xFFFD)

    pos = differ = 0
    for frame in frames:
        feed(_PRIVATE_CSI_RE.sub("", stream[pos:frame["pos"]]))
        pos = frame["pos"]
        mine = ["".join(cell_text(screen.buffer[y][x].data)
                        for x in range(cols)).rstrip()
                for y in range(rows)]
        cur = [screen.cursor.y, min(screen.cursor.x, cols - 1)]
        if mine != frame["rows"] or cur != frame["cur"]:
            differ += 1
            if differ == 1:
                print(f"first difference after stream offset {pos}: "
                      f"pyte cursor {cur}, conhost {frame['cur']}")
                for y in range(rows):
                    if mine[y] != frame["rows"][y]:
                        print(f"  row {y} pyte   : {mine[y][:100]!r}")
                        print(f"  row {y} conhost: {frame['rows'][y][:100]!r}")
    print(f"conhost: {len(frames)} frames, {differ} differ from pyte")
    return 1 if differ else 0


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--conhost-child":
        conhost_child(sys.argv[2], sys.argv[3])
        return 0
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("path", help="a render_capture.py capture or a dump")
    ap.add_argument("--rows", type=int, default=0, metavar="N")
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--conhost", action="store_true")
    args = ap.parse_args()
    src = load(args.path)
    status = replay(src, args.raw, args.rows)
    if args.conhost:
        status |= compare_conhost(src)
    return status


if __name__ == "__main__":
    sys.exit(main())
