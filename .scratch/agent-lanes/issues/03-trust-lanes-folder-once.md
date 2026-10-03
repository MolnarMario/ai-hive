# Trust a repository's lanes folder once

Status: needs-triage

Every new lane is a new folder, so Claude shows its trust dialog once per
lane (spec-v3 Step 0 check 1). A50 suggested trusting `<repo>.lanes` once:
a trusted parent folder covers its children.

## Why it needs its own design

It writes to the user's Claude configuration (`~/.claude.json`), outside AI
Hive's own files:
- Claude rewrites that file from its own in-memory copy, so an outside edit
  can be lost or can clobber a newer one (the e2e test already works around
  a trust entry vanishing).
- Trust is a security decision. AI Hive should ask the user once per
  repository, not decide it.

## Proposal

- When the first lane of a repository is created, offer "Trust this
  repository's lanes folder in Claude Code" in a non-modal notice.
- Write the entry the way Claude Code itself does, and audit it.
- Never trust anything without that click.
