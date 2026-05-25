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
- **Don't record `type(exc).__name__` alone.** `TriggerResult.error` is what the runs UI shows; before `summarize_exception` existed, a `CalledProcessError` surfaced with zero context. Use `summarize_exception(exc)` at every `result.finish("error", ...)` site — it redacts PATs and includes stderr (with stdout fallback for `git push` rejections).
- **Never pass `-S` to `git commit`.** `configure_repo_signing` already sets `commit.gpgsign=true` on the cloned repo, so `-S` adds nothing in production. But `-S` *overrides* per-repo config — tests that set `commit.gpgsign=false` (no signing key in CI) will still try to sign and fail with `CalledProcessError rc=128 "No secret key"`. The local mac dev loop hides this because the global `~/.gitconfig` has a signing key. Always rely on the repo-level `commit.gpgsign` setting.
- **PAT redaction applies to ANY logged stderr from git.** Remote URLs use `https://x-access-token:<PAT>@host` — auth-failure stderr can echo the PAT. Wrap every `logger.*("stderr=%s", ...)` line with `_redact_pat(...)`, even debug ones.
- **Validate LLM "resolved" file content before pushing.** The model can echo `<<<<<<<`/`=======`/`>>>>>>>` markers back in a `resolve` decision. Run `_contains_conflict_markers` on every returned file and abort the merge if any line matches — never push markered content.
