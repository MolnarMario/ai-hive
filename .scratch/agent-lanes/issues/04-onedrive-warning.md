# Warn when a repository's lanes would land in OneDrive

Status: needs-triage

Lanes are created beside the repository (`<parent>\<repo>.lanes\`). When the
repository sits in a OneDrive-synced folder (Documents and Desktop are often
redirected there), every lane gets synced too: a full checkout per agent,
uploaded and downloaded while agents work, plus file locks from the sync
client that can make `git worktree remove` fail.

## Proposal

- When the Agent lanes switch is on and a lane is about to be created under
  a OneDrive root (`%OneDrive%`, `%OneDriveConsumer%`,
  `%OneDriveCommercial%`), show a one-time notice per repository with the
  path and the reason.
- Don't block lane creation: the user decides.
- Audit it as `LANE-WARN onedrive`.
