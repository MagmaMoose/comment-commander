# Common mistakes

Add a one-liner when a bug takes >1h.

- **PAT leaks.** `_redact_pat` in `processor.py` strips PAT from subprocess args — don't bypass.
- **Push races.** Per-PR lock in `_process_pr_locked` fixed concurrent push races (v1.6.1). Don't remove.
- **Reply loops.** Bot replies always carry `<!-- comment-commander -->`; `/process` filters by it.
- **Login case.** GitHub logins are case-insensitive — use `_parse_login_set` (lowercases).
- **`SSH_SIGNING_PRIVATE_KEY` keeps whitespace.** `_require` strips other envs; PEM keys break if stripped.
- **`/health` is filtered from access logs.** Filter any new high-frequency endpoint too.
- **Merge follow-up needs unshallow.** Triage clones `--depth 1`; `_resolve_merge_conflicts` must `git fetch --unshallow` before merging or git can't compute a merge base. The "unshallow" exit code is non-zero on already-complete repos — log and continue, do NOT abort the flow.
- **Never push a half-resolved merge.** If LLM returns `resolve` but misses any conflicted path, treat as abort and `git merge --abort`. Partial pushes wedge the PR worse than leaving the conflict for a human.
- **Loop guard for merge resolution.** Lives in `_process_pr_locked` so it only runs in our own trigger — never wire it to a `pull_request` webhook without adding a marker on the resolve-commit, or it will fan-out forever.
