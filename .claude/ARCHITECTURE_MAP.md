# Architecture

**Webhook:** `main.webhook` verifies HMAC, dedupes, returns 202, hands off to background `processor.process_jobs` → per-PR lock → clone branch → for each unresolved Copilot thread: `llm.decide()` (fix/dismiss/skip) → apply files + signed commit → resolve thread → single push, cleanup.

**Manual:** `POST /process` → `process_pr_manual` (bypasses bot/involved filters; bot replies marked `<!-- comment-commander -->` so re-runs never loop).

**Multi-instance:** `Settings.instances` (github.com always; GHE iff all `GHE_*` set). `find_instance_for_host` picks PAT + identity per call. Same SSH key for both — pubkey must be a Signing Key on both accounts.

**State:** SQLite dedupe on PVC; `TriggerStore` for `/process` runs.
