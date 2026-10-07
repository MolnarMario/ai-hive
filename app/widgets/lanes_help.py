"""The "What are lanes?" explainer (lane scopes, Phase B).

Lanes and the integrator map one to one onto how a developer worked
before AI agents: a branch per person, say when it's done, someone merges
main in, runs the tests, opens a pull request and gets it reviewed. The
explainer says that in one table, because nothing else in the app does. Opened from the
workspace header's ⎇ Lanes toggle and from the New Agent dialog.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

TITLE = "What are lanes?"

_ROWS = (
    ("Lane",
     "A feature branch in its own folder (git worktree), beside the repo "
     "in <i>repo</i>.lanes. Its agent can't overwrite or reset anyone "
     "else's work."),
    ("Lane chip ⎇ ↑2 ±3 ✓",
     "A glance at git status: 2 commits of its own, 3 uncommitted files, "
     "and ✓ once its agent marked the work done, which turns the chip "
     "green. Amber or red means another lane, or main, changed the same "
     "files, and wins over green."),
    ("Lane notices",
     "A colleague saying \"I changed that file an hour ago\". AI Hive "
     "reads every lane every 15 seconds and runs a real merge in memory, "
     "so the agent hears about a conflict before anyone merges."),
    ("Task done",
     "A developer saying \"ready for review\". When an agent finishes its "
     "task it commits with a last line of just Task done. Nothing ships "
     "until you ask the integrator, or pick Ship finished lanes on its "
     "lane chip."),
    ("Integrator",
     "The teammate who owns the merge: an agent you pick with Make "
     "integrator. When you ask it to, it combines the finished lanes into "
     "one pull request, fixes conflicts, bumps the version, runs the full "
     "test suite, gets a code review and fixes what it finds, then "
     "merges."),
)


def explainer_html() -> str:
    rows = "".join(
        f"<tr><td style='padding:3px 10px 3px 0'><b>{term}</b></td>"
        f"<td style='padding:3px 0'>{what}</td></tr>"
        for term, what in _ROWS)
    return (
        "<p>A lane is how a developer worked before AI: a branch per "
        "person, say when it's done, merge main in, run the tests, open a "
        "pull request, get it reviewed. AI Hive does the same for "
        "agents.</p>"
        f"<table>{rows}</table>"
        "<p>Working with one agent at a time, or agents in different "
        "repositories? A lane is then just a branch, and you can open the "
        "pull request from it yourself.</p>"
        "<p>The ⎇ Lanes toggles only decide whether a new agent starts with "
        "its own lane. An agent keeps its lane until you close its card, "
        "which removes the lane when it holds nothing unmerged.</p>")


def show_lanes_explainer(parent=None) -> QMessageBox:
    """Open the explainer without blocking (like the lane notices) and
    return it. It deletes itself when closed."""
    box = QMessageBox(QMessageBox.Icon.Information, TITLE, "", parent=parent)
    box.setTextFormat(Qt.TextFormat.RichText)
    box.setText(explainer_html())
    # QMessageBox wraps at a narrow default; the table needs the room
    box.setStyleSheet("QLabel#qt_msgbox_label { min-width: 560px; }")
    box.setModal(False)
    box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    box.setObjectName("LanesExplainer")
    box.show()
    return box
