"""AI worker process: `python -m doi.ai.worker`.

A separate process from the API, collector and paper engine. It reads recorded data
from PostgreSQL, runs event detection and meetings, and writes recommendations.
It never submits paper orders (humans approve through the API) and never calls Dhan.
"""

from __future__ import annotations

import asyncio
import logging
import os

from doi.ai.agents import sync_registry
from doi.ai.service import AIWorker, build_ai
from doi.app.runtime import build_runtime
from doi.core.config import Settings
from doi.core.logging import configure_logging

log = logging.getLogger(__name__)


async def main() -> int:
    settings = Settings.from_env()
    flt = configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    for s in (settings.xai_api_key, settings.dhan_access_token):
        if s is not None:
            flt.add_secret(s.get_secret_value())
    if settings.database_url is None:
        log.error("the AI worker needs DATABASE_URL (recorded data, budget and meeting memory live in PostgreSQL)")
        return 2
    rt = await build_runtime(settings)
    dsn = settings.database_url.get_secret_value()
    await asyncio.to_thread(sync_registry, dsn)
    rt.ai = build_ai(settings, dsn, kill_switch=lambda: bool(rt.controls and rt.controls.kill_switch()))
    log.info("AI worker started (provider %s)", "LIVE" if rt.ai.live else "DISABLED: meetings will abort with AI_UNAVAILABLE")
    await AIWorker(rt).run_forever(float(os.environ.get("AI_WORKER_INTERVAL_S", "60")))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
