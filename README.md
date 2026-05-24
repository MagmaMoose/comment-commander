# Comment Commander

A self-hosted webhook receiver that watches your GitHub PRs for Copilot review comments, triages them with a cheap LLM, and pushes **verified, SSH-signed commits** attributed to you — no GitHub Actions, no bot author.

Built so you can spend your Claude Code tokens on writing new code, not on chasing Copilot review threads.

## What it does

For every Copilot review comment on a PR you've subscribed via webhook:

1. Verifies the GitHub webhook signature, dedupes the delivery, and returns `202` immediately.
2. Clones the PR branch into a temp directory using a PAT.
3. For each unresolved Copilot thread:
   - Asks the configured LLM to classify the comment as **fix**, **dismiss**, or **skip**.
   - **fix** — writes the model's file changes, makes a signed commit, and resolves the thread after pushing.
   - **dismiss** — replies briefly explaining why and resolves the thread.
   - **skip** — replies that the bot couldn't safely fix it and leaves the thread open for you.
4. Pushes once at the end.
5. **Smart follow-up merge resolution** (kill-switch: `MERGE_CONFLICT_RESOLUTION=false`). After the push, fetches the PR's base branch and attempts a merge. On conflict, sends each conflicted file (with markers) to the LLM and asks for full resolved contents. If the model returns a complete resolution it's committed (signed) and pushed as a single `fix(merge): …` commit; if the model declines or its resolution is partial, `git merge --abort` runs and the PR is left as-is — never pushes a half-resolved state.
6. Cleans up the temp directory.

Commits are signed with an SSH ed25519 key that matches `~/.gitconfig`, so they show as verified on GitHub.

## Manual trigger (`POST /process`)

In addition to passively listening for webhooks, you can ask the bot to re-walk an entire PR:

```bash
curl -X POST https://comment-commander.magmamoose.com/process \
  -H "X-Trigger-Token: $GITHUB_WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"pr_url": "https://github.com/CalebSargeant/infra/pull/242"}'
```

Differences vs the webhook path:
- Every author counts — humans, Copilot, other review bots — not just the `BOT_LOGINS` allow-list.
- The `INVOLVED_USERS` whitelist is **bypassed** (manual = explicit intent — even PRs you didn't author can be processed this way).
- Thread starters only; reply-comments (`in_reply_to_id` set) are skipped.
- Bot replies carry a hidden `<!-- comment-commander -->` marker so re-runs ignore them and never loop.
- Resolved threads are still skipped (same as the webhook flow).

Auth reuses `GITHUB_WEBHOOK_SECRET` (no new vault entry needed). The shorthand form `owner/repo#N` and GHE URLs (`https://pinkroccade.ghe.com/org/repo/pull/N`) are also accepted.

## Subscribe a repo or org (`POST /setup-webhook`)

Programmatically register the webhook on a target so you don't have to click through the GitHub UI:

```bash
# Single repo
curl -X POST https://comment-commander.magmamoose.com/setup-webhook \
  -H "X-Trigger-Token: $GITHUB_WEBHOOK_SECRET" \
  -d '{"target": "https://github.com/CalebSargeant/infra"}'

# Whole org (every repo in it auto-fires)
curl -X POST https://comment-commander.magmamoose.com/setup-webhook \
  -H "X-Trigger-Token: $GITHUB_WEBHOOK_SECRET" \
  -d '{"target": "https://github.com/magmamoose"}'

# GHE org
curl -X POST https://comment-commander.magmamoose.com/setup-webhook \
  -H "X-Trigger-Token: $GITHUB_WEBHOOK_SECRET" \
  -d '{"target": "https://pinkroccade.ghe.com/some-org"}'
```

Auth and instance-detection follow the same rules as `/process`. The PAT for the matched instance must have the right scope: `admin:repo_hook` (or classic `repo` w/ admin) for repo targets, `admin:org_hook` for orgs.

## Multi-instance (GitHub.com + Enterprise)

Configure GHE alongside github.com by setting `GHE_HOST`, `GHE_PAT`, `GHE_AUTHOR_NAME`, `GHE_AUTHOR_EMAIL`. Each incoming webhook (and each `/process` call) is routed to the matching instance based on the URL host. Commits to GHE are made with the GHE identity; commits to github.com use the github.com identity. The same SSH signing key works on both as long as the public half is registered as a Signing Key on both accounts.

## Involvement whitelist (webhook only)

`INVOLVED_USERS` (comma-separated case-insensitive logins) restricts the webhook flow to PRs where one of those logins has authored or committed. So if a colleague opens a PR you have nothing in, the bot stays silent; the moment you push a commit there, the next webhook fires the bot. `/process` bypasses this filter entirely.

## LLM provider

Pluggable via `LLM_PROVIDER`:

| Provider     | `LLM_PROVIDER` | Default model            |
|--------------|----------------|--------------------------|
| DeepSeek     | `deepseek`     | `deepseek-chat`          |
| Anthropic    | `anthropic`    | `claude-haiku-4-5`       |
| OpenAI       | `openai`       | `gpt-4o-mini`            |
| OpenRouter   | `openrouter`   | `deepseek/deepseek-chat` |

Override the model with `LLM_MODEL` and the base URL with `LLM_BASE_URL` (e.g. for a local Ollama or a corporate gateway).

## Configuration

Secrets come from two sources, both projected into the pod as a Kubernetes Secret. The app reads everything from env vars.

### OCI Vault — non-signing secrets

Three secrets in `vault-prod` (eu-amsterdam-1), referenced by the existing `oci-vault` `ClusterSecretStore`:

| OCI Vault secret name                       | Purpose                                                                                |
|---------------------------------------------|----------------------------------------------------------------------------------------|
| `comment-commander-github-webhook-secret`   | HMAC secret you configure on the GitHub webhook                                        |
| `comment-commander-github-pat`              | Fine-grained PAT (`Contents: read/write`, `Pull requests: read/write`) for clone + API |
| `comment-commander-llm-api-key`             | API key for the chosen LLM provider                                                    |

### 1Password Connect — SSH signing key

The same SSH key used in `~/repos/magmamoose/.gitconfig` for local git signing. Synced via `OnePasswordItem` ([k8s/base/onepassworditem.yaml](k8s/base/onepassworditem.yaml)) — the resulting Kubernetes Secret's `private key` field is mapped to `SSH_SIGNING_PRIVATE_KEY` in the pod env.

Prereq: the 1Password Connect service account in the cluster must have read access to the vault holding the item.

### Pod environment

Non-sensitive runtime knobs (set on the deployment):

| Variable                    | Default                                                |
|-----------------------------|--------------------------------------------------------|
| `GIT_AUTHOR_NAME`           | `CalebSargeant`                                        |
| `GIT_AUTHOR_EMAIL`          | `4991715+CalebSargeant@users.noreply.github.com`       |
| `LLM_PROVIDER`              | `deepseek`                                             |
| `LLM_MODEL`                 | provider default                                       |
| `LLM_BASE_URL`              | provider default                                       |
| `BOT_LOGINS`                | `copilot[bot],github-copilot[bot]`                     |
| `ALLOWED_REPOSITORIES`      | `""` (all)                                             |
| `MAX_FILE_BYTES`            | `180000`                                               |
| `MAX_COMMENTS_PER_EVENT`    | `10`                                                   |
| `DRY_RUN`                   | `false`                                                |
| `MERGE_CONFLICT_RESOLUTION` | `true` (set `false` to disable the post-push merge follow-up) |
| `LOG_LEVEL`                 | `INFO`                                                 |
| `DEDUPE_DB_PATH`            | `/var/lib/comment-commander/deliveries.db`             |

## GitHub webhook setup

Create a repository (or org) webhook:

- **Payload URL:** `https://<your-tunnel-host>/webhook`
- **Content type:** `application/json`
- **Secret:** the value stored in `comment-commander-github-webhook-secret`
- **Events:** `Pull request review comments` and `Pull request reviews`

The endpoint returns `202 Accepted` within milliseconds; triage happens in a background task.

## Signing identity

This service uses the **same identity** as your local `~/.gitconfig` (specifically the magmamoose-scoped overlay, `~/repos/magmamoose/.gitconfig`):

- `user.name = CalebSargeant`
- `user.email = 4991715+CalebSargeant@users.noreply.github.com`
- `gpg.format = ssh`
- `user.signingkey = ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKF6tb49g9qIHBxqSJihG6xHnYmJfCuD34Pb7qKmsBqZ`

The private half of that key lives in the 1Password "GitHub Authentication CalebSargeant" item in the Sargeant account. 1Password Connect syncs it into the cluster as the `comment-commander-signing-key` Secret; the deployment maps the `private key` field to `$SSH_SIGNING_PRIVATE_KEY`.

If the 1P operator names the field differently in your environment (e.g. with a different label), adjust [k8s/base/deployment.yaml](k8s/base/deployment.yaml) — the `valueFrom.secretKeyRef.key` value is the only thing that needs to change.

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

## Build & deploy

CI/release is handled by [`calebsargeant/semantic-release@v1`](https://github.com/calebsargeant/semantic-release) — wired up in `.github/workflows/`:

- **CI** (`ci.yml`) — runs pytest on every PR, and builds a `pr-<N>` image only when files inside the image build context change.
- **Release** (`release.yml`) — semantic-versioned release on push to `main`; publishes `ghcr.io/magmamoose/comment-commander:v*`.

The image is built via `docker-bake.hcl` (multi-arch: `linux/amd64,linux/arm64`).

To apply manifests (assumes the OCI Vault `ClusterSecretStore` from the infra repo is already deployed):

```bash
kustomize build k8s/overlays/prod | kubectl apply -f -
```

The overlay expects:

- A `ghcr-pull-secret` in the `comment-commander` namespace.
- The `oci-vault` `ClusterSecretStore` (managed by the infra repo) able to read the four OCI Vault secrets above.
- A 256Mi `ReadWriteOnce` `PersistentVolumeClaim` for the dedupe DB.

## Exposing the webhook

Cloudflare Tunnel is the recommended path:

```bash
cloudflared tunnel create comment-commander
cloudflared tunnel route dns comment-commander comment-commander.<your-domain>
# Apply cloudflared-tunnel.yaml as the tunnel config
```

## Security notes

- The SSH signing private key is fetched from OCI Vault by ExternalSecrets, written to the container's filesystem with `0600` perms, and used only by `ssh-keygen -Y sign`. It is never logged.
- HMAC signature verification runs before any payload parsing.
- The PAT used for cloning is `Contents` + `Pull requests` scoped; use a fine-grained PAT restricted to the repos you actually want this to touch.
- Delivery dedupe is persisted in SQLite on a PVC so GitHub webhook retries can't double-commit across pod restarts.
- This is a single-tenant tool. Multi-tenanting it later will require per-tenant signing identities and secret isolation.

## Known limits

- Single-file context per comment by default. The LLM may still return multi-file changes; the processor applies any path inside the cloned repo.
- One commit per actionable comment. No squashing.
- Forks are handled only if the PAT can push to the head repo.
