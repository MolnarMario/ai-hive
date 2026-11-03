"""Capture what a real Claude Code sends to a card, for a rendering bug.

Runs `claude --settings {"tui": "default"}` (the classic renderer AI Hive
selects) in a pywinpty pseudo-console of the given size, inside a fresh
scratch folder, has it print a long reply, then plays one scenario and saves
every chunk it read with its time. `tools/render_replay.py` feeds the result
through a TerminalView. docs/agents/terminal-rendering.md says which scenario
reproduces which bug.

    .venv\\Scripts\\python.exe tools\\render_capture.py 30 155 out.json --scenario rows

Scenarios, played after the reply:
  ctrl-o   Ctrl+O twice, two times: the transcript view in and out, a full
           repaint of a frame taller than the screen
  rows     one row shorter and back, two times (request_repaint now)
  cols     one column narrower and back, two times (the old request_repaint,
           which left a copy of the top row each time)
  menu     the / menu and the @ menu, each opened, closed with Escape, then
           a one-row wiggle (the stranded input box and its heal)
  recap    two more prompts, then blur for 200 s and focus again (Claude's
           idle recap needs 3 prompts and 3 minutes blurred)

This launches a real claude and costs a few cents of usage per run. It is a
developer tool: the smoke suite never runs it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time

REPLY = """\
**1. The Work heading**

The heading block reads as floaty because nothing separates it from the first project. It has the same background as AI Hive's dark panel, no rule, and the intro paragraph runs straight into the screenshot. The Process section below is a tinted band with borders, so Work is the only section with no edge.

I'd do two things:
- Put a 1px rule across the full width under the stats row, and a second hairline under the intro paragraph. A bit more space after the hairline makes the heading block a header strip, and the projects start clearly below it.
- Trim the intro to two lines. As it stands, the paragraph is the same width and weight as the app descriptions, so it blends into them.

If that's still not enough, the stronger fix is to box each project in a panel. The heading would then clearly sit outside the cards. I'd hold that back because it adds visual weight to five big features.

**2. The 2016 degree**

I'd put it in both places, with different jobs.

In the timeline, add one entry between 2015 and 2017. It's a fact and it belongs in the sequence. The timeline would have five items, so I'd check that it still fits in one row on a phone.

In the paragraphs, I'd put it in the second one, not the first. The first is the story of every seat a product passes through, and a degree would interrupt it. The second explains why you work the way you do, and an engineering background fits there.

Want me to implement the rules and the timeline entry, and then you tweak the paragraph wording?
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("rows", type=int)
    ap.add_argument("cols", type=int)
    ap.add_argument("out", help="where to write the capture (JSON)")
    ap.add_argument("--scenario", default="ctrl-o",
                    choices=("none", "ctrl-o", "rows", "cols", "menu", "recap"))
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--reply-file", help="markdown for Claude to print "
                    "instead of the built-in reply")
    args = ap.parse_args()

    from winpty import PtyProcess

    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("CLAUDECODE", "CLAUDE_CODE",
                                        "CLAUDE_EFFORT"))}
    claude = os.path.expanduser(r"~\.local\bin\claude.exe")
    cwd = tempfile.mkdtemp(prefix="aihive-render-capture-")
    rows, cols = args.rows, args.cols
    proc = PtyProcess.spawn(
        [claude, "--settings", json.dumps({"tui": "default"}),
         "--model", args.model],
        dimensions=(rows, cols), cwd=cwd, env=env)
    chunks: list = []
    marks: dict = {}
    lock = threading.Lock()

    def reader():
        try:
            while True:
                data = proc.read(4096)
                if not data:
                    break
                with lock:
                    chunks.append((time.time(), data))
        except Exception:
            pass

    threading.Thread(target=reader, daemon=True).start()

    def text() -> str:
        with lock:
            return "".join(d for _, d in chunks)

    def mark(name: str) -> None:
        marks[name] = len(text())

    def wait_for(needle: str, seconds: float) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            if needle in text().replace(" ", ""):
                return True
            time.sleep(0.2)
        return False

    def prompt(line: str, settle: float) -> None:
        proc.write("\x1b[200~" + line + "\x1b[201~")
        time.sleep(0.8)
        proc.write("\r")
        time.sleep(settle)

    time.sleep(4)
    if "trust" in text().lower():       # the trust prompt defaults to "exit"
        proc.write("\x1b[B")
        time.sleep(0.3)
        proc.write("\r")
        time.sleep(3)
    wait_for("forshortcuts", 20)
    time.sleep(1)
    if args.scenario == "recap":        # the recap needs three real prompts
        prompt("Reply with just the word: one", 12)
        prompt("Reply with just the word: two", 12)
    reply = REPLY
    if args.reply_file:
        with open(args.reply_file, encoding="utf-8") as fh:
            reply = fh.read()
    prompt("Reply with exactly the markdown below, verbatim, and nothing "
           "else. Do not use tools.\n\n" + reply, 40)
    mark("reply")

    def wiggle(r: int, c: int, n: int) -> None:
        proc.setwinsize(r, c)
        time.sleep(0.12)
        proc.setwinsize(rows, cols)
        time.sleep(3)
        if r < rows:
            # where AI Hive's request_repaint writes REPAINT_DONE once the
            # redraw settles; render_replay.py puts the marker there
            mark(f"repaint done {n}")

    for n in range(2):
        if args.scenario == "ctrl-o":
            proc.write("\x0f")
            time.sleep(2)
            proc.write("\x0f")
            time.sleep(3)
        elif args.scenario == "rows":
            wiggle(rows - 1, cols, n + 1)
        elif args.scenario == "cols":
            wiggle(rows, cols - 1, n + 1)
        elif args.scenario == "menu":
            key = "/@"[n]
            proc.write(key)
            time.sleep(2)
            proc.write("\x1b")
            time.sleep(0.4)
            proc.write("\x7f")
            time.sleep(2.5)
            mark(f"{key} stranded")
            wiggle(rows - 1, cols, n + 1)
        mark(f"{args.scenario} {n + 1}")
    if args.scenario == "recap":
        proc.write("\x1b[O")
        time.sleep(200)
        proc.write("\x1b[I")
        time.sleep(20)
        mark("recap")
    time.sleep(2)
    with lock:
        saved = list(chunks)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"kind": "capture", "rows": rows, "cols": cols,
                   "scenario": args.scenario, "marks": marks,
                   "chunks": saved}, fh)
    try:
        proc.write("\x03")
        time.sleep(0.5)
        proc.write("\x03")
        time.sleep(1)
        proc.terminate(force=True)
    except Exception:
        pass
    print(f"{args.out}: {len(saved)} chunks, "
          f"{sum(len(d) for _, d in saved)} chars, marks {marks}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
