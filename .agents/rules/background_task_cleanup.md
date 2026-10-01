# Background Process Cleanup Rules

## Overview
When running shell commands or background tasks, agents must prevent orphan child processes from lingering in the background. Lingering child processes trigger the gear badge (⚙️) in the AI Hive workspace sidebar and card header.

## Rules

1. **Synchronous Execution**:
   - For commands with a known, bounded execution time (such as running test suites, builds, or diagnostics), run them synchronously or set a sufficient `WaitMsBeforeAsync` so the process completes and terminates within the step.

2. **Proactive Task Monitoring & Cleanup**:
   - When launching asynchronous background commands (`run_command` async), track the active task IDs.
   - Once a background task finishes its job, fails, or is no longer needed, inspect the running task list using `manage_task(Action='list')`.
   - Explicitly terminate any completed or hanging background tasks using `manage_task(Action='kill', TaskId=...)` before concluding the response.

3. **Prevent Lingering AI Hive Gear Badges (⚙️)**:
   - Ensure all child processes spawned by tools or scripts are terminated prior to ending the turn, keeping the AI Hive sidebar free of lingering background process badges.
