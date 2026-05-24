# Session: 2026-05-24 — smart follow-up merge conflict resolution

## What we did
Added a post-push merge-resolution step inside `_process_pr_locked`. After the existing comment-fix push, the processor unshallows, fetches base, attempts `git merge --no-ff --no-commit`, and on conflict sends conflicted files (with markers) to a new `LLMProvider.resolve_merge` method for full resolved contents. Signed merge commit + push, or `git merge --abort` if the LLM declines/under-covers.

## Key decisions
- **In-process, not a new webhook**: avoids loops (resolve-commit doesn't fan back) and matches the user's "follow-up" framing. Webhook-driven design (`pull_request` synchronize) was rejected to keep scope tight.
- **Full file to LLM, not hunk-by-hunk**: reuses existing FileChange shape; simpler prompt; broad coverage. Partial coverage → abort.
- **Two-method LLM provider**: shared `_chat(system, user) -> str` powers both `decide` (triage) and `resolve_merge` (merge), so adding the second flow didn't duplicate any HTTP code.
- **Kill switch defaults ON** (`MERGE_CONFLICT_RESOLUTION=false` to disable). Added a `_truthy_default` helper because the existing `_truthy` defaults to False.

## Files changed
- [src/llm/base.py](src/llm/base.py): `MergeConflictContext`, `MergeResolution`, `MERGE_SYSTEM_PROMPT`, `build_merge_user_prompt`, `parse_merge_resolution`.
- [src/llm/providers.py](src/llm/providers.py): extracted `_chat`, added `resolve_merge` (auto-derived in `BaseProvider`).
- [src/llm/__init__.py](src/llm/__init__.py): re-exports.
- [src/config.py](src/config.py): `merge_conflict_resolution: bool` setting, `_truthy_default` helper.
- [src/processor.py](src/processor.py): `ReviewJob.base_branch`, threaded through `extract_jobs`/manual flow, new `_resolve_merge_conflicts` + helpers (`_list_conflicted_files`, `_has_staged_changes`, `_read_conflicted_snapshots`, `_write_merge_resolutions`, `_abort_merge`, `_commit_with_subject`, `_normalise_commit_subject`).
- [src/slack.py](src/slack.py): `notify_merge_resolution` with resolve/abort variants.
- [tests/conftest.py](tests/conftest.py): `merge_conflict_resolution=False` default; `StubProvider.resolve_merge`; `base` block in `comment_payload`.
- [tests/test_merge_conflict.py](tests/test_merge_conflict.py): real bare-origin + clone integration tests for no-op, clean merge, LLM resolve, LLM abort, partial-resolution abort.
- [tests/test_llm.py](tests/test_llm.py): parser/builder tests + `resolve_merge` wire test.
- README.md, PROJECT_INDEX.json, .claude/COMMON_MISTAKES.md updated.

## Gotchas hit
- **`git fetch --unshallow` on an already-complete repo** exits non-zero with `"--unshallow on a complete repository does not make sense"`, not `"is not a shallow repository"` as I first assumed. First pattern-match was too strict and aborted the whole flow in tests with shallow-but-tiny repos. Fix: log + continue on any unshallow failure.
- Existing `_commit_signed` carried an unused `settings` arg — left alone (out of scope) and factored shared logic into `_commit_with_subject` instead.

## Next steps
- Watch logs for real-world merge attempts on PRs like CalebSargeant/zoey#41 (DIRTY mergeStatus) and tune the system prompt if the LLM is too eager to abort.
- Consider a `pull_request` webhook handler later for base-drift conflicts (when our commits didn't cause the conflict). Would need a loop guard (commit-subject marker like `chore(merge):` or `fix(merge):` already exists in our subjects — could filter on that).
