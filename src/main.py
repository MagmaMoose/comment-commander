"""FastAPI entrypoint. Verifies, dedupes, then hands off to the background."""
from __future__ import annotations

import hmac
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response

from config import Settings
from dedupe import DeliveryStore
from github_client import verify_signature
from llm import build_provider
from processor import extract_jobs, parse_pr_url, process_jobs, process_pr_manual
from signing import install_ssh_signing_key
from slack import SlackNotifier

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
        "boot llm_provider=%s llm_model=%s author=%s <%s> dry_run=%s allow_repos=%s slack=%s",
        settings.llm_provider, settings.llm_model,
        settings.git_author_name, settings.git_author_email,
        settings.dry_run,
        sorted(settings.allowed_repositories) or "*",
        "on" if slack.enabled else "off",
    )
    app = FastAPI()
    app.state.settings = settings
    app.state.signing_key_path = signing_key_path
    app.state.provider = provider
    app.state.deliveries = deliveries

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
        trigger_id = uuid.uuid4().hex[:12]
        logger.info(
            "manual /process accepted trigger=%s repo=%s pr=%s",
            trigger_id, repo.full_name, pr_number,
        )
        background_tasks.add_task(
            _run_manual,
            repo=repo, pr_number=pr_number, trigger_id=trigger_id,
        )
        return Response(
            content=json.dumps({
                "status": "processing",
                "trigger_id": trigger_id,
                "repo": repo.full_name,
                "pr": pr_number,
            }),
            media_type="application/json",
            status_code=202,
        )

    def _run_manual(*, repo, pr_number: int, trigger_id: str) -> None:
        try:
            process_pr_manual(
                repo, pr_number,
                settings,
                trigger_id=trigger_id,
                provider=provider,
                signing_key_path=signing_key_path,
                slack=slack,
            )
            logger.info("manual trigger processed trigger=%s", trigger_id)
        except Exception:  # noqa: BLE001
            logger.exception("manual_trigger_failed trigger=%s", trigger_id)

    return app


# Run with: uvicorn main:create_app --factory --host 0.0.0.0 --port 8000
