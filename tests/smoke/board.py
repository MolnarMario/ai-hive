"""The shared coordination board: activity-log rotation into the archive and
the roster's task column."""

import os
import re
import shutil
import tempfile
from pathlib import Path

from .harness import SCRATCH_CWD, check


def _entries(path) -> list[str]:
    """The top-level log entries in a board or archive file, decoded strictly
    so a file that isn't valid UTF-8 fails the test."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [ln for ln in text.splitlines() if ln.startswith("- ")]


def _line_count(*paths) -> int:
    n = 0
    for p in paths:
        if os.path.isfile(p):
            n += len(Path(p).read_text(encoding="utf-8").splitlines())
    return n


def _message(entry: str) -> str:
    # "- [Agent 2] 2026-10-02 20:30 note 003" -> "note 003"
    return re.sub(r"^- \[[^\]]*\] \S+ \S+ ", "", entry)


def test_board_rotation():
    """board.md keeps the newest LOG_KEEP activity entries and moves older
    ones to board-archive.md. The archive is append-only and written first,
    so across the two files every entry exists exactly once and in order: a
    rotation never loses one or duplicates one, even when a write fails."""
    from PySide6.QtWidgets import QApplication
    from app import coordination as co
    from app.orchestrator_bridge import OrchestratorBridge
    from app.workspace_manager import WorkspaceManager

    QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-board-"))
    keep = co.LOG_KEEP

    def note(i):
        return f"note {i:03d}"

    # --- fills up, then rotates one entry per append ---
    board = co.WorkspaceBoard(str(tmp / "ws"))
    board.append_activity("A1", "mojibake \udc81 survives the archive")
    for i in range(1, keep):
        board.append_activity("A1", note(i))
    check("board-rotate: no archive while the log fits in LOG_KEEP",
          not os.path.exists(board.archive_path)
          and len(_entries(board.path)) == keep)
    board.update_roster([{"name": "A1", "role": "", "provider": "claude",
                          "model": "", "status": "idle", "task": "keep me"}])
    board.append_activity("A1", note(keep))
    on_board, archived = _entries(board.path), _entries(board.archive_path)
    check("board-rotate: the board keeps the newest LOG_KEEP entries",
          len(on_board) == keep and _message(on_board[0]) == note(1)
          and _message(on_board[-1]) == note(keep),
          (len(on_board), on_board[:1], on_board[-1:]))
    check("board-rotate: the oldest entry moves to the archive",
          len(archived) == 1 and "mojibake" in archived[0], archived)
    body = Path(board.path).read_text(encoding="utf-8")
    check("board-rotate: the roster survives a rotation",
          "keep me" in body and body.count(co.ROSTER_BEGIN) == 1)
    check("board-rotate: the board points at the archive",
          body.count(co.ARCHIVE_NOTE) == 1)
    check("board-rotate: the archive starts with its header",
          Path(board.archive_path).read_text(encoding="utf-8")
          .startswith(co.ARCHIVE_HEADER))

    # --- many more appends: append-only, nothing lost, nothing doubled ---
    total = keep * 3 + 7
    prefix_kept, plus_one = True, True
    for i in range(keep + 1, total):
        before = Path(board.archive_path).read_bytes()
        lines = _line_count(board.path, board.archive_path)
        board.append_activity("A1", note(i))
        prefix_kept &= Path(board.archive_path).read_bytes().startswith(before)
        plus_one &= (_line_count(board.path, board.archive_path) == lines + 1)
    check("board-rotate: the archive is append-only", prefix_kept)
    check("board-rotate: each append adds exactly one line across both files",
          plus_one)
    every = [_message(e) for e in
             _entries(board.archive_path) + _entries(board.path)]
    check("board-rotate: every entry is in exactly one file, in order",
          every[1:] == [note(i) for i in range(1, total)]
          and "mojibake" in every[0], (len(every), every[:3], every[-3:]))
    body = Path(board.path).read_text(encoding="utf-8")
    check("board-rotate: the board stays at LOG_KEEP entries",
          len(_entries(board.path)) == keep)
    check("board-rotate: the archive note is added once, not per rotation",
          body.count(co.ARCHIVE_NOTE) == 1
          and Path(board.archive_path).read_text(encoding="utf-8")
          .count(co.ARCHIVE_HEADER) == 1)
    check("board-rotate: read_log_tail still returns the newest entries",
          [_message(e) for e in board.read_log_tail(2)]
          == [note(total - 2), note(total - 1)], board.read_log_tail(2))

    # --- a board from before rotation: cut down on its next append ---
    legacy = co.WorkspaceBoard(str(tmp / "legacy"))
    os.makedirs(legacy.dir)
    old = (["- [Agent 2] 2026-07-06 11:42 old 000",
            "  written by hand, continued on a second line",
            "  - and a nested bullet"]
           + [f"- [Agent 2] 2026-07-06 11:43 old {i:03d}"
              for i in range(1, 261)])
    preamble = "## Activity log\n\n_Agents append their activity below._\n"
    original = ("# AI Hive: workspace coordination board\n\n"
                f"{co.ROSTER_BEGIN}\n{co.ROSTER_END}\n\n"
                + preamble + "\n".join(old) + "\n")
    Path(legacy.path).write_text(original, encoding="utf-8")
    lines = _line_count(legacy.path)
    legacy.append_activity("A1", "first note after the upgrade")
    on_board = _entries(legacy.path)
    archive_text = Path(legacy.archive_path).read_text(encoding="utf-8")
    body = Path(legacy.path).read_text(encoding="utf-8")
    check("board-rotate: a legacy board is cut to LOG_KEEP on its next append",
          len(on_board) == keep
          and _message(on_board[-1]) == "first note after the upgrade"
          and _message(on_board[0]) == f"old {261 - keep + 1:03d}",
          (len(on_board), on_board[:1]))
    check("board-rotate: a multi-line entry moves whole",
          "\n".join(old[:4]) in archive_text
          and "written by hand" not in body and "nested bullet" not in body)
    check("board-rotate: the legacy preamble stays and gains the archive note",
          preamble + co.ARCHIVE_NOTE + "\n- " in body, body[:400])
    header_lines = len(co.ARCHIVE_HEADER.splitlines())
    check("board-rotate: the first rotation's note says to read the archive "
          "only if needed", "only if you need history" in body, body[:400])
    check("board-rotate: legacy line count is conserved",
          _line_count(legacy.path, legacy.archive_path)
          == lines + 1 + 1 + header_lines,   # the entry, the note, the header
          (_line_count(legacy.path, legacy.archive_path), lines))

    scaffolded = co.WorkspaceBoard(str(tmp / "scaffold"))
    scaffolded.ensure()
    check("board-rotate: a new board says to read the archive only if needed",
          "only if you need history"
          in Path(scaffolded.path).read_text(encoding="utf-8"))

    # --- an archive that can't be written skips the rotation, not the note ---
    blocked = co.WorkspaceBoard(str(tmp / "blocked"))
    for i in range(keep):
        blocked.append_activity("A1", note(i))
    os.makedirs(blocked.archive_path)   # a folder where the file should go
    ok = blocked.append_activity("A1", "still logged")
    on_board = _entries(blocked.path)
    check("board-rotate: an unwritable archive keeps every entry on the board",
          ok and len(on_board) == keep + 1
          and _message(on_board[-1]) == "still logged", (ok, len(on_board)))

    # --- a board write that fails after the archive append rolls it back ---
    rb = co.WorkspaceBoard(str(tmp / "rollback"))
    for i in range(keep):
        rb.append_activity("A1", note(i))
    board_bytes = Path(rb.path).read_bytes()
    rb._write = lambda text: False      # board.md locked by another process
    ok = rb.append_activity("A1", "board was locked")
    check("board-rotate: a failed board write reports failure", ok is False)
    check("board-rotate: a failed first rotation leaves no archive entries",
          _entries(rb.archive_path) == []
          and Path(rb.path).read_bytes() == board_bytes)
    del rb._write
    rb.append_activity("A1", note(keep))
    archive_bytes = Path(rb.archive_path).read_bytes()
    board_bytes = Path(rb.path).read_bytes()
    check("board-rotate: the archive header survives a rolled-back first try",
          Path(rb.archive_path).read_text(encoding="utf-8")
          .startswith(co.ARCHIVE_HEADER)
          and [_message(e) for e in _entries(rb.archive_path)] == [note(0)])
    rb._write = lambda text: False
    rb.append_activity("A1", "board was locked again")
    check("board-rotate: a failed board write truncates the archive back",
          Path(rb.archive_path).read_bytes() == archive_bytes
          and Path(rb.path).read_bytes() == board_bytes)
    del rb._write

    # --- the log_activity tool rotates through the same serialized append ---
    mgr = WorkspaceManager()
    ws = mgr.create_workspace("Rotate", project_path=str(tmp / "bridge"))
    bridge = OrchestratorBridge(mgr, active_ws=lambda: ws.id)
    for i in range(keep + 2):
        bridge._dispatch("log_activity", {"agent": "B1", "message": note(i)},
                         ws=ws.id)
    check("board-rotate: log_activity rotates the board",
          len(_entries(ws.board.path)) == keep
          and [_message(e) for e in _entries(ws.board.archive_path)]
          == [note(0), note(1)])
    check("board-rotate: the archive lives next to board.md",
          os.path.dirname(ws.board.archive_path) == ws.board.dir)
    mgr.remove_workspace(ws.id)

    shutil.rmtree(tmp, ignore_errors=True)


def test_board_roster_task():
    """The roster's task column shows what the card header shows: the live AI
    title, else the assigned task (it read "-" for almost every agent). A
    title change rewrites the roster without marking the session dirty,
    since the title is transient. A multi-line task stays on its table row.
    A recompute that changes nothing does not rewrite board.md."""
    from PySide6.QtWidgets import QApplication
    from app.process_worker import AgentKind, build_spec
    from app.workspace_manager import WorkspaceManager

    QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-roster-"))
    mgr = WorkspaceManager()
    ws = mgr.create_workspace("Roster", project_path=str(tmp))
    a = mgr.add_terminal(ws.id, build_spec(AgentKind.CLAUDE, "Agent 1",
                                           cwd=SCRATCH_CWD), autostart=False)

    def task_cell():
        text = Path(ws.board.path).read_text(encoding="utf-8")
        row = next((ln for ln in text.splitlines()
                    if ln.startswith("| Agent 1 |")), "")
        return row.rstrip(" |").rsplit("|", 1)[-1].strip()

    check("roster: no task and no title renders as -", task_cell() == "-",
          task_cell())
    a.set_task("Fix the parser")
    check("roster: with no AI title, the assigned task shows",
          a.roster_row()["task"] == "Fix the parser"
          and task_cell() == "Fix the parser", task_cell())
    dirty = []
    mgr.dirty.connect(lambda: dirty.append(1))
    a.set_ai_title("Recolor the badge")
    check("roster: the live AI title wins over an assigned task, like the card",
          a.roster_row()["task"] == "Recolor the badge" == a.summary())
    check("roster: a title change rewrites the board roster",
          task_cell() == "Recolor the badge", task_cell())
    check("roster: a title change never marks the session dirty", not dirty)
    a.set_task("")
    check("roster: clearing the task keeps the AI title",
          task_cell() == "Recolor the badge", task_cell())
    a.set_ai_title("")
    check("roster: no title and no task renders as - again",
          task_cell() == "-", task_cell())
    a.set_task("step one\nstep two | three")
    check("roster: a multi-line task stays on one table row",
          task_cell() == "step one step two / three", task_cell())

    # every status and title tick recomputes the roster; only a change writes
    writes = []
    real_write = ws.board._write
    ws.board._write = lambda text: writes.append(1) or real_write(text)
    mgr._recompute(ws.id)
    check("roster: a recompute that changes no row does not rewrite the board",
          writes == [], len(writes))
    a.set_ai_title("Tidy the parser")
    check("roster: a row change rewrites the board once",
          len(writes) == 1 and task_cell() == "Tidy the parser",
          (len(writes), task_cell()))
    del ws.board._write

    for agent in mgr.all_agents():
        agent.dispose()
    mgr.remove_workspace(ws.id)
    shutil.rmtree(tmp, ignore_errors=True)


def test_board_survives_hand_edits():
    """An agent that edits board.md by hand can remove the roster markers or
    the log heading. The next roster update or append must keep every line
    it finds. Both used to rebuild the scaffold and drop the whole log."""
    from app import coordination as co

    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-boardedit-"))
    title = "# AI Hive: workspace coordination board"
    rows = [{"name": "A1", "role": "", "provider": "claude", "model": "",
             "status": "idle", "task": "roster is back"}]

    # --- roster markers removed, five entries in the log ---
    board = co.WorkspaceBoard(str(tmp / "nomarkers"))
    os.makedirs(board.dir)
    entries = [f"- [Agent 2] 2026-10-01 10:0{i} kept entry {i}"
               for i in range(5)]
    Path(board.path).write_text(
        f"{title}\n\n## Agents\n\nan agent's own note\n\n## Activity log\n\n"
        + "\n".join(entries) + "\n", encoding="utf-8")
    board.update_roster(rows)
    body = Path(board.path).read_text(encoding="utf-8")
    check("board-edit: a roster update keeps every entry when the markers "
          "are gone", all(e in body for e in entries)
          and "an agent's own note" in body, body)
    check("board-edit: ...and puts the roster back once, under the title",
          body.startswith(title + "\n\n" + co.ROSTER_BEGIN)
          and body.count(co.ROSTER_BEGIN) == 1
          and body.count(co.ROSTER_END) == 1 and "roster is back" in body,
          body[:300])
    board.append_activity("A1", "after the repair")
    check("board-edit: the repaired board still takes appends",
          _entries(board.path)[:5] == entries
          and _message(_entries(board.path)[-1]) == "after the repair")

    # --- a lone marker left behind does not pair with the new roster ---
    lone = co.WorkspaceBoard(str(tmp / "lone"))
    os.makedirs(lone.dir)
    Path(lone.path).write_text(
        f"{title}\n\n## Activity log\n\n{entries[0]}\n{co.ROSTER_END}\n"
        f"{entries[1]}\n", encoding="utf-8")
    lone.update_roster(rows)
    lone.update_roster(rows)
    body = Path(lone.path).read_text(encoding="utf-8")
    check("board-edit: a lone marker is replaced, not paired",
          body.count(co.ROSTER_BEGIN) == 1 and body.count(co.ROSTER_END) == 1
          and entries[0] in body and entries[1] in body
          and body.index(co.ROSTER_END) < body.index(co.LOG_HEADER), body)

    # --- the log heading removed: a fresh one goes at the end ---
    nohead = co.WorkspaceBoard(str(tmp / "noheading"))
    os.makedirs(nohead.dir)
    original = (f"{title}\n\n{co.ROSTER_BEGIN}\n## Agents\n{co.ROSTER_END}\n\n"
                "notes an agent wrote by hand\n"
                "- [Agent 3] 2026-10-01 09:00 an entry under no heading\n")
    Path(nohead.path).write_text(original, encoding="utf-8")
    nohead.append_activity("A1", "after the heading was lost")
    body = Path(nohead.path).read_text(encoding="utf-8")
    check("board-edit: an append keeps a board that lost its log heading",
          body.startswith(original), body)
    tail = body[len(original):]
    check("board-edit: ...and adds a fresh heading, then the entry",
          tail.startswith("\n" + co.LOG_HEADER + "\n- [A1] ")
          and tail.rstrip("\n").endswith("after the heading was lost"), tail)

    shutil.rmtree(tmp, ignore_errors=True)
