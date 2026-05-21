"""FastAPI entrypoint. Verifies, dedupes, then hands off to the background."""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response

from pathlib import Path

from config import Settings
from dedupe import DeliveryStore
from github_client import verify_signature
from llm import build_provider
from processor import extract_jobs, process_jobs
from signing import install_ssh_signing_key

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("comment-commander")


def create_app(
    settings: Settings | None = None,
    *,
    provider: Any = None,
    signing_key_path: str | Path | None = None,
    deliveries: DeliveryStore | None = None,
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
    app = FastAPI()
    app.state.settings = settings
    app.state.signing_key_path = signing_key_path
    app.state.provider = provider
    app.state.deliveries = deliveries

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
            raise HTTPException(status_code=403, detail="invalid_signature")

        delivery = x_github_delivery or "unknown"
        if x_github_delivery and not deliveries.claim(x_github_delivery):
            logger.info("duplicate_delivery_ignored: %s", delivery)
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
            )
        except Exception:  # noqa: BLE001 - background task must not crash the server
            logger.exception("background_task_failed delivery=%s", delivery)

    return app


# Run with: uvicorn main:create_app --factory --host 0.0.0.0 --port 8000
