# Integrating a lane

AI Hive's integration queue (`app/integration.py`, agent lanes Phase 3) sends
the workspace's integrator agent one brief per lane the user submits. The
brief embeds the **Checklist** section below and the **Test command** line,
read from the base branch, with these placeholders filled in: `{integrate}`
(the branch to create), `{sha}` (the pinned commit), `{base_ref}`
(`origin/main`), `{base}` (`main`) and `{test_command}`.

Lanes never bump the version or touch the CHANGELOG or the README check
count. The integrator does, once per pull request, in step 5.

Test command: `.venv\Scripts\python.exe tests\smoke_test.py`

## Checklist

1. In your own lane, create the branch from the pinned commit:
   `git switch -c {integrate} {sha}`. Work only on that branch.
2. `git fetch origin`, then merge {base_ref} into it and resolve every
   conflict. Never rebase and never force-push.
3. Run the FULL suite, including the real-claude e2e test:
   `{test_command}`. Don't shorten its timeouts.
4. Run `/code-review` on the branch and fix what it finds. Rerun the suite
   if you changed anything.
5. Bump `__version__` in `app/__init__.py`, add the matching `## x.y.z`
   section at the top of `CHANGELOG.md` (written for users, no em dash), and
   set the README check count from the suite's RESULT line.
6. Push with `git push -u origin {integrate}` and open the pull request with
   `gh pr create --base {base} --head {integrate}`. The PR body lists the
   suite result. List every failing check, and run it on {base_ref} to say
   whether it fails there too. Never decide yourself that a failure is
   acceptable.
7. Never run `gh pr merge`. Stop and report: the user approves the merge in
   AI Hive, and AI Hive merges only the commit you tested.
