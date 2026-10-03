# Run the quick suite on GitHub for every pull request

Status: needs-triage

The repository has no GitHub status checks (fix-and-merge-plan F38). Every
merge relies on the full suite run locally and pasted into the PR body. The
real-claude e2e test can't run on a hosted runner (it needs a logged-in
Claude), but everything else can.

## Proposal

- A workflow on `windows-latest` that installs `requirements.txt` and runs
  `python tests\smoke_test.py --quick` on every pull request to `main`.
- The offscreen Qt platform and the sandbox profile already make the suite
  headless and self-contained.
- Optionally, "Require status checks to pass" on `main` once it is stable.

## Open questions

- Runtime on a hosted runner (about 3 minutes locally).
- Whether ConPTY tests behave on the hosted image; skip them with a reason
  there if not, rather than loosening them.
