# comment-commander

Self-hosted FastAPI webhook receiver: triages GitHub Copilot PR review comments with a cheap LLM and pushes verified SSH-signed commits. Multi-instance (github.com + GHE). Python 3.12 / FastAPI / pytest, deployed to k8s behind Cloudflare Tunnel.

## Context

- **`PROJECT_INDEX.json`** (load on demand for any exploration / cross-module task) — module map, callgraph, hotspots.
- @.claude/ARCHITECTURE_MAP.md — how the pieces fit.
- @.claude/COMMON_MISTAKES.md — footguns.
- @.claude/QUICK_START.md — common commands.

`.claude/decisions/` and `.claude/sessions/` are on-demand only — load only when the task explicitly relates.

## [tooling]

- Summarise build/test/lint output; don't echo full stdout unless a failure needs it.
- grep/find/glob: return paths + relevant lines only.
- Shell output >50 lines → write to `.claude/last_output.txt`, reference by path.
- Prefer targeted Read (offset+limit) for `src/processor.py` (820 lines) and `src/main.py` (526 lines).
- Don't re-read a file you just edited — Edit/Write would have errored on failure.

## [maintenance]

- Bug >1h to fix → one-liner in `.claude/COMMON_MISTAKES.md`.
- Architectural decision → `.claude/decisions/YYYY-MM-DD-<topic>.md`.
- `PROJECT_INDEX.json` stale (new module / refactor) → regenerate affected sections only.
- Meaningful session → `.claude/sessions/YYYY-MM-DD-<slug>.md` from `TEMPLATE.md` (≤300 tokens).
- This file: ≤500 tokens. Push detail into on-demand files.
