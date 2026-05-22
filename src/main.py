"""FastAPI entrypoint. Verifies, dedupes, then hands off to the background."""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response

from config import Settings
from dedupe import DeliveryStore
from github_client import (
    GitHubClient,
    GitHubError,
    GitHubInstance,
    find_instance_for_host,
    verify_signature,
)
from llm import build_provider
from processor import extract_jobs, parse_pr_url, process_jobs, process_pr_manual
from signing import install_ssh_signing_key
from slack import SlackNotifier
from triggers import TriggerResult, TriggerStore

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# Configure root logging. Calling basicConfig is a no-op if a handler is
# already attached (e.g. uvicorn already installed one), so we install our
# own handler on the root logger explicitly.
_root = logging.getLogger()
if not any(isinstance(h, logging.StreamHandler) for h in _root.handlers):
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(LOG_FORMAT))
    _root.addHandler(_handler)
_root.setLevel(LOG_LEVEL)

# Uvicorn installs its own access logger with a different format and emits a
# line for every /health probe (kubelet hits it every 10s). Drop /health from
# access logs and align the format with ours so the timeline reads cleanly.
for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    lg = logging.getLogger(name)
    lg.handlers = []
    lg.propagate = True


class _DropHealthChecks(logging.Filter):
    """Suppress access-log lines for /health to keep the timeline readable."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        msg = record.getMessage()
        return ("GET /health" not in msg) and ('"GET /health' not in msg)


for name in ("uvicorn.access",):
    logging.getLogger(name).addFilter(_DropHealthChecks())

logger = logging.getLogger("comment-commander")


def create_app(
    settings: Settings | None = None,
    *,
    provider: Any = None,
    signing_key_path: str | Path | None = None,
    deliveries: DeliveryStore | None = None,
    slack: SlackNotifier | None = None,
) -> FastAPI:
    settings = settings or Settings.load()
    signing_key_path = signing_key_path or install_ssh_signing_key(
        settings.ssh_signing_private_key
    )
    provider = provider or build_provider(
        settings.llm_provider,
        settings.llm_api_key,
        settings.llm_model,
        settings.llm_base_url,
    )
    deliveries = deliveries or DeliveryStore(settings.dedupe_db_path)
    slack = slack or SlackNotifier(
        token=settings.slack_bot_token,
        channel=settings.slack_channel_id,
    )
    logger.info(
        "boot llm_provider=%s llm_model=%s instances=%s involved_users=%s "
        "allow_repos=%s dry_run=%s slack=%s",
        settings.llm_provider, settings.llm_model,
        [f"{i.name}@{i.host}({i.author_name})" for i in settings.instances],
        sorted(settings.involved_users) or "*",
        sorted(settings.allowed_repositories) or "*",
        settings.dry_run,
        "on" if slack.enabled else "off",
    )
    app = FastAPI()
    app.state.settings = settings
    app.state.signing_key_path = signing_key_path
    app.state.provider = provider
    app.state.deliveries = deliveries

    # Per-trigger results for manual /process runs — lets comment-commander-pro
    # poll GET /process/{trigger_id} for the final status + Slack message ids.
    trigger_store = TriggerStore()
    app.state.triggers = trigger_store

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        """Single-line per-request log (skips /health)."""
        start = time.perf_counter()
        response = await call_next(request)
        if request.url.path != "/health":
            logger.info(
                "%s %s %s %dms client=%s",
                request.method,
                request.url.path,
                response.status_code,
                int((time.perf_counter() - start) * 1000),
                (request.client.host if request.client else "-"),
            )
        return response

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhook")
    async def webhook(
        request: Request,
        background_tasks: BackgroundTasks,
        x_hub_signature_256: str | None = Header(default=None),
        x_github_event: str | None = Header(default=None),
        x_github_delivery: str | None = Header(default=None),
    ) -> Response:
        raw = await request.body()
        if not verify_signature(raw, x_hub_signature_256, settings.github_webhook_secret):
            logger.warning(
                "rejected webhook with bad signature delivery=%s event=%s",
                x_github_delivery or "?", x_github_event or "?",
            )
            raise HTTPException(status_code=403, detail="invalid_signature")

        delivery = x_github_delivery or "unknown"
        logger.info(
            "received webhook delivery=%s event=%s bytes=%d",
            delivery, x_github_event or "?", len(raw),
        )

        if x_github_delivery and not deliveries.claim(x_github_delivery):
            logger.info("duplicate delivery ignored delivery=%s", delivery)
            return Response(
                content=json.dumps({"status": "duplicate"}),
                media_type="application/json",
                status_code=200,
            )

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=f"invalid_json: {exc}") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="invalid_json")

        jobs = extract_jobs(payload, x_github_event or "", settings)
        if not jobs:
            logger.info(
                "no jobs extracted (filtered by event/login/repo) delivery=%s action=%s",
                delivery, payload.get("action"),
            )
            return Response(
                content=json.dumps({"status": "ignored"}),
                media_type="application/json",
                status_code=200,
            )

        background_tasks.add_task(
            _run_jobs,
            jobs=jobs,
            delivery=delivery,
        )
        return Response(
            content=json.dumps({"status": "processing", "jobs": len(jobs)}),
            media_type="application/json",
            status_code=202,
        )

    def _run_jobs(*, jobs: Any, delivery: str) -> None:
        try:
            process_jobs(
                jobs,
                settings,
                delivery=delivery,
                provider=provider,
                signing_key_path=signing_key_path,
                slack=slack,
            )
            logger.info("delivery processed delivery=%s", delivery)
        except Exception:  # noqa: BLE001 - background task must not crash the server
            logger.exception("background_task_failed delivery=%s", delivery)

    @app.post("/process")
    async def manual_process(
        request: Request,
        background_tasks: BackgroundTasks,
        x_trigger_token: str | None = Header(default=None),
    ) -> Response:
        """Manual entrypoint — give it a PR URL and it re-walks every
        unresolved review thread, regardless of who authored each comment.

        Auth: requires `X-Trigger-Token: <GITHUB_WEBHOOK_SECRET>` (same
        secret as the GitHub webhook — saves a second secret).
        Body:   {"pr_url": "https://github.com/owner/repo/pull/123"}
        """
        if not x_trigger_token or not hmac.compare_digest(
            x_trigger_token, settings.github_webhook_secret
        ):
            logger.warning("rejected /process call with bad token")
            raise HTTPException(status_code=403, detail="invalid_token")

        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=f"invalid_json: {exc}") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="invalid_body")

        parsed = parse_pr_url(body.get("pr_url") or "")
        if not parsed:
            raise HTTPException(status_code=400, detail="invalid_pr_url")
        repo, pr_number = parsed
        host = _host_from_pr_url(body.get("pr_url") or "")
        instance = (
            find_instance_for_host(host, settings.instances)
            if host
            else settings.instances[0]
        )
        if instance is None:
            raise HTTPException(
                status_code=400,
                detail=f"unknown_host: {host} (configured: {[i.host for i in settings.instances]})",
            )
        trigger_id = uuid.uuid4().hex[:12]
        logger.info(
            "manual /process accepted trigger=%s instance=%s repo=%s pr=%s",
            trigger_id, instance.name, repo.full_name, pr_number,
        )
        result = trigger_store.create(
            trigger_id=trigger_id,
            repo=repo.full_name,
            pr_number=pr_number,
            instance=instance.name,
        )
        background_tasks.add_task(
            _run_manual,
            instance=instance, repo=repo, pr_number=pr_number,
            trigger_id=trigger_id, result=result,
        )
        return Response(
            content=json.dumps({
                "status": "processing",
                "trigger_id": trigger_id,
                "instance": instance.name,
                "repo": repo.full_name,
                "pr": pr_number,
            }),
            media_type="application/json",
            status_code=202,
        )

    def _run_manual(
        *, instance: GitHubInstance, repo, pr_number: int, trigger_id: str,
        result: TriggerResult,
    ) -> None:
        try:
            process_pr_manual(
                instance, repo, pr_number,
                settings,
                trigger_id=trigger_id,
                provider=provider,
                signing_key_path=signing_key_path,
                slack=slack,
                result=result,
            )
            result.finish("ok")
            logger.info("manual trigger processed trigger=%s", trigger_id)
        except Exception as exc:  # noqa: BLE001
            result.finish("error", error=type(exc).__name__)
            logger.exception("manual_trigger_failed trigger=%s", trigger_id)

    @app.get("/process/{trigger_id}")
    def process_status(
        trigger_id: str,
        x_trigger_token: str | None = Header(default=None),
    ) -> Response:
        """Outcome of a manual /process run: status, decision counts and the
        Slack message ids it posted. Polled by comment-commander-pro.

        Auth: same X-Trigger-Token as /process. Returns 404 once a trigger
        has aged out of the in-memory store (or never existed)."""
        if not x_trigger_token or not hmac.compare_digest(
            x_trigger_token, settings.github_webhook_secret
        ):
            raise HTTPException(status_code=403, detail="invalid_token")
        result = trigger_store.get(trigger_id)
        if result is None:
            raise HTTPException(status_code=404, detail="unknown_trigger")
        return Response(
            content=json.dumps(result.as_dict()),
            media_type="application/json",
            status_code=200,
        )

    @app.post("/setup-webhook")
    async def setup_webhook(
        request: Request,
        x_trigger_token: str | None = Header(default=None),
    ) -> Response:
        """Provision the GitHub webhook for a repo or an org.

        Body: {"target": "https://github.com/owner/repo"}   # repo-scoped
              {"target": "https://pinkroccade.ghe.com/some-org"} # org-scoped

        Auth: X-Trigger-Token = GITHUB_WEBHOOK_SECRET (same secret already
        used by /process).

        The PAT for the matched instance must have the right scope:
          - repo-scoped webhook: `admin:repo_hook` (or `repo` for classic PATs)
          - org-scoped webhook:  `admin:org_hook`
        """
        if not x_trigger_token or not hmac.compare_digest(
            x_trigger_token, settings.github_webhook_secret
        ):
            raise HTTPException(status_code=403, detail="invalid_token")
        if not settings.public_webhook_url:
            raise HTTPException(
                status_code=500,
                detail="PUBLIC_WEBHOOK_URL not configured on the deployment",
            )

        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=f"invalid_json: {exc}") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="invalid_body")

        target = body.get("target") or ""
        parsed = _parse_target(target)
        if not parsed:
            raise HTTPException(status_code=400, detail="invalid_target")
        host, owner, maybe_repo = parsed
        instance = find_instance_for_host(host, settings.instances)
        if instance is None:
            raise HTTPException(
                status_code=400,
                detail=f"unknown_host: {host} (configured: {[i.host for i in settings.instances]})",
            )

        events = ["pull_request_review", "pull_request_review_comment"]
        scope = f"{owner}/{maybe_repo}" if maybe_repo is not None else owner
        try:
            with GitHubClient.for_instance(instance) as gh:
                from github_client import RepositoryRef
                repo_ref = RepositoryRef(owner=owner, repo=maybe_repo) if maybe_repo else None
                # Idempotency: if a hook already targets our PUBLIC_WEBHOOK_URL,
                # return that one instead of failing with 422 "already exists".
                existing = _find_existing_hook(
                    gh, repo_ref, owner, settings.public_webhook_url,
                )
                if existing is not None:
                    logger.info(
                        "webhook already present instance=%s scope=%s hook_id=%s",
                        instance.name, scope, existing.get("id"),
                    )
                    return Response(
                        content=json.dumps({
                            "status": "already_exists",
                            "instance": instance.name,
                            "scope": scope,
                            "hook_id": existing.get("id"),
                            "webhook_url": settings.public_webhook_url,
                            "events": existing.get("events") or events,
                        }),
                        media_type="application/json",
                        status_code=200,
                    )
                if repo_ref is not None:
                    result = gh.create_repo_hook(
                        repo_ref,
                        webhook_url=settings.public_webhook_url,
                        secret=settings.github_webhook_secret,
                        events=events,
                    )
                else:
                    result = gh.create_org_hook(
                        owner,
                        webhook_url=settings.public_webhook_url,
                        secret=settings.github_webhook_secret,
                        events=events,
                    )
        except GitHubError as exc:
            logger.warning("setup-webhook failed scope=%s: %s", scope, exc)
            # Pass through 4xx so the caller can act on it (e.g. SAML SSO
            # 403 telling them to authorise the PAT for an enterprise org).
            status = exc.status_code or 502
            if status not in (401, 403, 404, 422):
                status = 502
            raise HTTPException(status_code=status, detail=str(exc)) from exc

        logger.info(
            "webhook provisioned instance=%s scope=%s hook_id=%s url=%s events=%s",
            instance.name, scope, result.get("id"),
            settings.public_webhook_url, events,
        )
        return Response(
            content=json.dumps({
                "status": "ok",
                "instance": instance.name,
                "scope": scope,
                "hook_id": result.get("id"),
                "webhook_url": settings.public_webhook_url,
                "events": events,
            }),
            media_type="application/json",
            status_code=201,
        )

    return app


def _find_existing_hook(gh, repo_ref, org_owner: str, webhook_url: str):
    """Return any existing hook whose `config.url` matches `webhook_url`."""
    try:
        hooks = gh.list_repo_hooks(repo_ref) if repo_ref else gh.list_org_hooks(org_owner)
    except GitHubError:
        # If listing isn't permitted (e.g. PAT lacks read scope on hooks),
        # fall through and let the create call decide.
        return None
    for hook in hooks:
        config = hook.get("config") if isinstance(hook, dict) else None
        if isinstance(config, dict) and config.get("url") == webhook_url:
            return hook
    return None


def _host_from_pr_url(url: str) -> str | None:
    """Extract host from a PR URL. None for shorthand `owner/repo#N`."""
    match = re.match(r"^https?://([^/]+)/", url.strip())
    return match.group(1) if match else None


def _parse_target(target: str) -> tuple[str, str, str | None] | None:
    """Parse a setup-webhook target into (host, owner, repo_or_None).

    Accepts:
      https://github.com/owner/repo            -> ("github.com", "owner", "repo")
      https://github.com/owner/repo/anything   -> ("github.com", "owner", "repo")
      https://github.com/some-org              -> ("github.com", "some-org", None)
      owner/repo                               -> defaults host to github.com
    """
    target = (target or "").strip()
    if not target:
        return None
    match = re.match(
        r"^(?:https?://(?P<host>[^/]+)/)?(?P<owner>[^/#]+)(?:/(?P<repo>[^/#?]+))?",
        target,
    )
    if not match:
        return None
    host = match.group("host") or "github.com"
    owner = match.group("owner")
    repo = match.group("repo")
    return (host.lower(), owner, repo)


# Run with: uvicorn main:create_app --factory --host 0.0.0.0 --port 8000
