"""Wire settings -> token source -> transport -> schedulers -> adapter -> universe."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import httpx

from doi.core.config import Settings
from doi.core.errors import ConfigError, DoiError
from doi.core.models import UnderlyingRef
from doi.data.dhan.adapter import DhanMarketData
from doi.data.dhan.token import TokenSource
from doi.data.dhan.transport import DhanTransport
from doi.data.instruments.master import InstrumentMaster
from doi.data.provider import MarketDataProvider
from doi.intelligence.strategy_configs.loader import IndexConfig, load_index_configs

log = logging.getLogger(__name__)


@dataclass
class Runtime:
    configs: dict[str, IndexConfig]
    provider: MarketDataProvider | None = None
    token: TokenSource | None = None
    universe: dict[str, UnderlyingRef] = field(default_factory=dict)
    universe_errors: dict[str, str] = field(default_factory=dict)
    master_version: str | None = None
    kill_switch: bool = False

    def resolve(self, master: InstrumentMaster, today: date | None = None) -> None:
        self.master_version = master.version.version
        self.universe.clear()
        self.universe_errors.clear()
        for key, cfg in self.configs.items():
            try:
                self.universe[key] = master.resolve_underlying(key, cfg.exchange_family, cfg.master_symbol, today)
            except DoiError as e:
                self.universe_errors[key] = str(e)


async def download_master(url: str, timeout_s: float = 90.0) -> InstrumentMaster:
    async with httpx.AsyncClient() as c:
        resp = await c.get(url, timeout=timeout_s)
        resp.raise_for_status()
        return InstrumentMaster.parse(resp.content, datetime.now(UTC))


async def build_runtime(settings: Settings) -> Runtime:
    configs = load_index_configs(settings.strategy_config_dir)
    rt = Runtime(configs=configs)
    if not settings.dhan_client_id:
        log.warning("DHAN_CLIENT_ID not set; market data disabled")
        return rt
    if not (settings.dhan_access_token or settings.dhan_token_file):
        raise ConfigError("set DHAN_ACCESS_TOKEN or DHAN_TOKEN_FILE")
    rt.token = TokenSource(
        value=settings.dhan_access_token.get_secret_value() if settings.dhan_access_token else None,
        path=settings.dhan_token_file,
    )
    transport = DhanTransport(
        base_url=settings.dhan_base_url, client_id=settings.dhan_client_id, token=rt.token.get, timeout_s=settings.http_timeout_s
    )
    live, hist = DhanMarketData.build_schedulers(
        transport,
        cooldown_s=settings.chain_cooldown_s,
        concurrency=settings.chain_max_concurrency,
        hist_cooldown_s=settings.historical_cooldown_s,
        hist_concurrency=settings.historical_max_concurrency,
    )
    rt.provider = DhanMarketData(transport, live=live, historical=hist)
    rt.resolve(await download_master(settings.instrument_master_url), datetime.now(UTC).date())
    return rt
