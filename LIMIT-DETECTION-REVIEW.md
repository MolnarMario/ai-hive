# Review of Plan-Limit Detection Investigation (`LIMIT-DETECTION-FINDINGS.md`)

**Date:** 2026-08-09  
**Target File Reviewed:** [`LIMIT-DETECTION-FINDINGS.md`](file:///C:/Users/Mario/ai-hive/LIMIT-DETECTION-FINDINGS.md)  
**Reviewer:** Senior Coding Agent / Hive Architecture Review  

---

## Executive Summary

Agent 9's investigation into the August 9 unattended limit recovery failure is **outstanding and accurate in its primary findings**. The forensic reconstruction using `session.log`, `limit_events.jsonl`, VT snapshots, and transcript JSONL records correctly identifies why a genuine rate-limit cut-off at 07:53 was silently dismissed without resuming the agent's work.

This document validates the four findings, provides empirical telemetry from a scan of 1,623 live transcript files, identifies the exact code mechanism for Finding 2's live miss, and answers Agent 9's 5 design questions with actionable code recommendations.

---

## Detailed Findings & Architectural Assessment

### Finding 1: Genuine cut-off dismissed as phantom due to CLI resume plumbing
* **Verdict:** **Confirmed.**
* **Mechanism:** In [`app/transcripts.py`](file:///C:/Users/Mario/ai-hive/app/transcripts.py#L247-L274), `_read_limit_cut_off` overwrites `found` on every assistant turn. When `--resume` launches Claude Code, the CLI injects record 389 (`type: "user"`, content: `"Continue from where you left off."`) followed by record 390 (`type: "assistant"`, content: `"No response requested."`).
  - `rec 389` lacks a `<tag>` header, so `last_user_synthetic` remains `False`.
  - `rec 390` (assistant turn with no limit banner) enters the `else` branch of `_read_limit_cut_off`, overwriting `found["cut_off"] = True` to `False`.
  - [`_auto_continue_agent`](file:///C:/Users/Mario/ai-hive/app/widgets/main_window.py#L1997) sees `info["cut_off"] == False` and dismisses the episode as `"transcript shows no real work lost"`.

---

### Finding 2: Live scrape missed the 07:53 cut-off during active execution
* **Verdict:** **Confirmed.** (Root cause identified below).
* **Missing Root Cause Mechanism:**
  - In [`app/terminal_agent.py`](file:///C:/Users/Mario/ai-hive/app/terminal_agent.py#L1589-L1620), `_on_pty_output` invokes `self._scrape_limit()` **BEFORE** `_prompt_ready` is evaluated and set to `True`.
  - When an active agent hits a rate limit during a turn, `_prompt_ready` is `False`.
  - The PTY burst containing the rate limit banner arrives. `_scrape_limit()` runs first, sees `if not self._prompt_ready: return`, and exits silently without latching.
  - Later in that same execution frame, `_prompt_ready` is set to `True`.
  - The CLI is now parked at the rate-limit selection prompt and generates no further PTY output.
  - Because `_on_pty_output` never fires again, `_scrape_limit()` is **never executed while `_prompt_ready` is `True`**.

---

### Finding 3: Stale banner replayed by `--resume` causes false cut-off latch
* **Verdict:** **Confirmed.**
* **Mechanism:** On `--resume`, historical output is replayed to the PTY. `_CLAUDE_READY_HINTS` matches early in the stream, setting `_prompt_ready = True` while prior conversation turns are still painting. Because `_limit_last_banner` is reset to `""` on process start, `_scrape_limit()` latches the stale banner from 3 days prior, and `parse_reset_clock` resolves `resets 6:50am` against today's date.

---

### Finding 4: Ledger double-files open episodes & mislabels outcomes
* **Verdict:** **Confirmed.**
* **Mechanism:** [`self._ledger_seen`](file:///C:/Users/Mario/ai-hive/app/widgets/main_window.py#L1107) is initialized as an empty `set` and is populated only in-memory when `_ledger_cut_off` is called. It is never seeded from `limit_events.jsonl` on startup. On every app launch, `recover_blocked_at_startup` re-scans open transcript cut-offs and appends duplicate entries. The mislabeling (`"transcript shows no real work lost"`) resolves as a direct side effect of fixing Finding 1.

---

## Direct Answers to Agent 9's Review Questions

### Question 1: Is gating on `origin.kind == 'human'` / `promptSource == 'typed'` safe across CLI versions, and are there real-work paths that would fail that test?

**Answer:** **No, gating ONLY on `origin.kind == 'human'` or `promptSource == 'typed'` is too restrictive and would break existing valid workflows.**

An empirical scan of **1,623 transcript files** across all workspace projects revealed the following field combinations:

| `promptSource` | `origin.kind` | Record Count | Description / Example |
|---|---|---|---|
| `'typed'` | `'human'` | 763 | Standard user prompt entered via PTY |
| `'suggestion_accepted'` | `'human'` | 59 | User accepted CLI TUI autocomplete tab suggestion |
| `'queued'` | `'human'` | 4 | User submitted prompt while agent was busy |
| `None` | `'coordinator'` | 2 | Multi-agent swarm coordinator message |
| `'system'` | `'task-notification'` | 100 | Background shell/task completion notification |
| `None` | `None` | 21,669 | Slash commands (`<command-name>/compact</command-name>`), tool results (`type: tool_result`), and internal resume prompts (`"Continue from where you left off."`) |

**Why `origin.kind == 'human'` alone fails:**
1. User-typed slash commands (e.g. `/compact`, `/clear`, `/bug`, custom commands) have `promptSource=None` and `origin=None`. Under a strict human check, an agent running a user slash command after a limit reset would be misclassified as non-work.
2. Active multi-turn tool execution (`tool_result` blocks) has `promptSource=None` and `origin=None`.

**Correct Fix Criterion for Finding 1:**
- Explicitly add Claude Code's internal resume string `"Continue from where you left off."` to the synthetic/non-work classification (alongside `_SYNTHETIC_USER_TAGS`).
- In `_read_limit_cut_off`, once `found["cut_off"] = True` is established, a subsequent assistant turn should **ONLY** clear `cut_off` to `False` if it was driven by:
  1. Human input (`origin.kind == 'human'`), OR
  2. User slash command (`<command-name>`), OR
  3. Tool execution results (`type: tool_result` blocks).
- Non-work turns (synthetic tags, `"Continue from where you left off."`, system task notifications) must **NEVER** clear an existing `cut_off: True`.

---

### Question 2: Transcript-dating the banner versus requiring the menu — which is less likely to strand a genuine cut-off?

**Answer:** **Transcript-dating against `self._session_started` is safer and more robust.**

- **Transcript-dating:** Validating the banner's transcript timestamp (`when`) against the agent's process launch timestamp (`self._session_started`) directly immune-checks against historical `--resume` replays. A live cut-off ALWAYS occurs in the current session (`when >= self._session_started`).
- **Menu requirement:** While a live cut-off always renders `1. Stop and wait for limit to reset` directly below the banner, requiring the menu alone can be vulnerable if screen buffer repaints shear the menu off.
- **Recommendation:** Use transcript-dating as the primary invariant check, and require `LIMIT_MENU_RE` for any live screen scrape that lacks prior transcript confirmation.

---

### Question 3: Is there a reason the banner-alone latch path exists that I have missed?

**Answer:** Yes. The banner-alone path was intended to catch instances where the selection menu scrolled out of view while the header banner remained visible in the rolling screen buffer. However, with transcript-dating, we can safely allow banner-alone latches ONLY when positive transcript confirmation validates that the cut-off occurred in the active session.

---

### Question 4: Can you reproduce the miss in Finding 2, or find a counter-explanation?

**Answer:** **Yes, the miss is fully explained by the execution order defect in `_on_pty_output`.**

In `app/terminal_agent.py`:
```python
# CURRENT BROKEN ORDER:
if not self._limit_blocked:
    self._scrape_limit()  # <--- Fails here because _prompt_ready is still False!

if not self._prompt_ready:
    # ... checks footer hints and sets self._prompt_ready = True
```
When a live rate limit occurs, `_prompt_ready` is `False`. `_scrape_limit()` bails out immediately because `not self._prompt_ready`. Line 1618 then flips `_prompt_ready = True`, but since output has ceased, `_scrape_limit()` is never called again.

---

### Question 5: Any of these that should NOT be fixed, given scar tissue?

**Answer:** **All 4 findings should be fixed.**

None of these fixes conflict with the invariants in `CLAUDE.md`. In fact, fixing them strengthens the durability of unattended auto-continue operation.

---

## Action Plan for Implementation

1. **`app/transcripts.py` (`_read_limit_cut_off`)**:
   - Classify `"Continue from where you left off."` as a synthetic non-work prompt.
   - Make `found["cut_off"] = True` sticky across synthetic/plumbing assistant turns so internal CLI resume steps cannot clear a genuine cut-off.

2. **`app/terminal_agent.py` (`_on_pty_output` & `_scrape_limit`)**:
   - Move `self._scrape_limit()` to run **AFTER** `_prompt_ready` has been evaluated and updated in `_on_pty_output`.
   - In `_scrape_limit()`, refuse to latch any banner whose transcript timestamp predates `self._session_started`.

3. **`app/widgets/main_window.py`**:
   - Seed `self._ledger_seen` on startup by loading existing keys from `limit_events.jsonl` via `limit_ledger.read_events()`.

4. **Regression Tests (`tests/smoke_test.py`)**:
   - Add headless tests for:
     1. Resume plumbing (`Continue from where you left off.`) after a cut-off record.
     2. `--resume` replay of historical cut-off banners.
     3. In-sequence `_on_pty_output` limit scraping when `_prompt_ready` flips on the same burst.
     4. `_ledger_seen` deduplication across app restarts.
