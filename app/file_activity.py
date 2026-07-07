"""Per-agent file-activity attribution, parsed from Claude transcripts.

AI Hive does not observe OS file writes, so it cannot attribute a write to a
terminal from the outside (see README's "Shared agent awareness"). The ONE
reliable source of "which files did THIS agent touch" is the agent's own Claude
conversation transcript: each Claude agent is pinned to a conversation at
~/.claude/projects/<encoded-cwd>/<session-id>.jsonl (see transcripts.py), a
newline-delimited JSON log in which every `{"type":"tool_use"}` block records a
tool the agent ran. Verified against real transcripts on the target machine:

  Edit / Write / MultiEdit / NotebookEdit  -> input.file_path  (file MODIFIED)
  Read                                     -> input.file_path  (file READ)
  Agent / Task                             -> input.{subagent_type, description}
                                              (a Claude SUB-AGENT it spawned)

A Claude sub-agent's OWN tool calls do NOT appear in the parent transcript (no
sidechain entries in this CLI version), and the sub-agent's transcript is
ephemeral and not addressable by AI Hive — so sub-agents are surfaced as labeled
nodes only, with no file attribution (files roll up to the parent agent).

Qt-free (pure stdlib + transcripts.py) so tests and the model layer can use it
headlessly, like coordination.py / orchestration.py.
"""

import json
import os
from dataclasses import dataclass, field

from . import transcripts

# tool_use names, matched case-exactly. Both "Agent" and "Task" are accepted for
# the sub-agent tool because the CLI has used both spellings across versions.
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_READ_TOOLS = {"Read"}
_SUBAGENT_TOOLS = {"Agent", "Task"}

# Safety valve: a runaway transcript must never stall the UI thread. When a file
# exceeds this, only its trailing bytes are scanned (the most RECENT activity),
# and `truncated` is set so the UI can note it.
_MAX_SCAN_BYTES = 12 * 1024 * 1024


@dataclass
class FileAccess:
    """One file an agent touched. `edited` wins over `read` for edge styling —
    a file that was both read and edited reads as edited."""
    path: str            # original path as recorded (may be Windows-style)
    edited: bool = False
    read: bool = False
    count: int = 0       # number of tool_use touches, for weighting/debug

    @property
    def basename(self) -> str:
        return os.path.basename(self.path.replace("\\", "/")) or self.path


@dataclass
class SubAgent:
    """A Claude Task/Agent sub-agent the parent spawned. No file attribution."""
    subagent_type: str = ""
    description: str = ""


@dataclass
class AgentActivity:
    files: dict = field(default_factory=dict)   # normalized key -> FileAccess
    subagents: list = field(default_factory=list)  # list[SubAgent]
    truncated: bool = False   # transcript too big; only the tail was scanned

    def edited_files(self) -> list:
        return [f for f in self.files.values() if f.edited]

    def read_only_files(self) -> list:
        return [f for f in self.files.values() if f.read and not f.edited]


def _key(file_path: str) -> str:
    """Dedup key: normalize separators + case-fold (Windows paths are
    case-insensitive, and the same file can appear as C:\\... and c:\\...)."""
    return os.path.normpath(file_path.replace("\\", "/")).lower()


def _note_file(act: AgentActivity, file_path: str, edited: bool) -> None:
    if not file_path:
        return
    k = _key(file_path)
    fa = act.files.get(k)
    if fa is None:
        fa = FileAccess(path=file_path)
        act.files[k] = fa
    fa.count += 1
    if edited:
        fa.edited = True
    else:
        fa.read = True


def _consume_entry(obj: dict, act: AgentActivity) -> None:
    msg = obj.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name")
        inp = block.get("input")
        inp = inp if isinstance(inp, dict) else {}
        if name in _EDIT_TOOLS:
            _note_file(act, inp.get("file_path") or "", edited=True)
        elif name in _READ_TOOLS:
            _note_file(act, inp.get("file_path") or "", edited=False)
        elif name in _SUBAGENT_TOOLS:
            act.subagents.append(SubAgent(
                subagent_type=str(inp.get("subagent_type") or "").strip(),
                description=str(inp.get("description") or "").strip()))


def parse_transcript(path: str) -> AgentActivity:
    """Parse one transcript JSONL into an AgentActivity. Never raises: a missing
    file yields an empty result, a malformed line (e.g. the partial final line of
    a transcript being written live) is skipped."""
    act = AgentActivity()
    try:
        size = os.path.getsize(path)
    except OSError:
        return act
    try:
        with open(path, "rb") as fh:
            if size > _MAX_SCAN_BYTES:
                fh.seek(size - _MAX_SCAN_BYTES)
                fh.readline()  # drop the partial line the seek landed inside
                act.truncated = True
            for raw in fh:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    continue  # partial/corrupt line — skip, keep scanning
                if isinstance(obj, dict):
                    _consume_entry(obj, act)
    except OSError:
        return act
    return act


def activity_for_agent(agent) -> AgentActivity | None:
    """Attribute files for a TerminalAgent. Returns None for agents with no
    parseable transcript (non-Claude providers, or a Claude agent with no pinned
    session yet); otherwise an AgentActivity (possibly empty if the agent has not
    acted yet)."""
    spec = getattr(agent, "spec", None)
    if spec is None or getattr(spec, "provider", "") != "claude":
        return None
    if not getattr(spec, "session_id", ""):
        return None
    path = transcripts.transcript_path(spec.cwd, spec.session_id)
    return parse_transcript(path)
