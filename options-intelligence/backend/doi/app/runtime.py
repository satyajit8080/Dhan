"""Wire settings -> token source -> transport -> schedulers -> adapter -> universe."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx

from doi.core.config import Settings
from doi.core.errors import ConfigError, DoiError
from doi.core.market import SessionCalendar, load_holidays
from doi.core.models import UnderlyingRef
from doi.data.dhan.adapter import DhanMarketData
from doi.data.dhan.collector import MarketFeedCollector
from doi.data.dhan.token import TokenSource
from doi.data.dhan.transport import DhanTransport
from doi.data.instruments.master import InstrumentMaster
from doi.data.provider import MarketDataProvider
from doi.db.migrate import migrate
from doi.intelligence.strategy_configs.loader import IndexConfig, load_index_configs
from doi.paper.engine import ExecutionPolicy, PaperExecution
from doi.paper.fills import FillModel
from doi.paper.ledger import Ledger
from doi.paper.service import PaperService
from doi.paper.store import PaperStore
from doi.risk.gate import RiskGate
from doi.risk.models import LiquidityLimits, RiskConfig

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
    master: InstrumentMaster | None = None
    paper: PaperExecution | None = None
    paper_store: PaperStore | None = None
    collector: MarketFeedCollector | None = None
    _paper_service: PaperService | None = field(default=None, repr=False)

    @property
    def paper_service(self) -> PaperService:
        if self._paper_service is None:
            self._paper_service = PaperService(self, self.paper_store)
        return self._paper_service

    def resolve(self, master: InstrumentMaster, today: date | None = None) -> None:
        self.master = master
        self.master_version = master.version.version
        self.universe.clear()
        self.universe_errors.clear()
        for key, cfg in self.configs.items():
            try:
                self.universe[key] = master.resolve_underlying(key, cfg.exchange_family, cfg.master_symbol, today)
            except DoiError as e:
                self.universe_errors[key] = str(e)


PAPER_ACCOUNT = "paper-1"


def build_gate(
    configs: dict[str, IndexConfig],
    *,
    fill_model: FillModel | None = None,
    risk: RiskConfig | None = None,
    calendar: SessionCalendar | None = None,
) -> RiskGate:
    limits = {
        k: LiquidityLimits(c.liquidity.max_spread_pct, c.liquidity.min_oi, c.liquidity.min_volume, c.liquidity.max_quote_age_s)
        for k, c in configs.items()
    }
    return RiskGate(risk or RiskConfig(), fill_model or FillModel(), calendar or SessionCalendar(), limits)


async def init_paper(rt: Runtime, settings: Settings, now: datetime) -> None:
    """In-memory engine, or PostgreSQL-backed: migrate, load (or create), reconcile, recover."""
    calendar = SessionCalendar(holidays=load_holidays(settings.holidays_file))
    if settings.database_url is None:
        rt.paper = build_paper(
            rt.configs, starting_capital=Decimal(str(settings.paper_starting_capital)), now=now,
            ledger_path=settings.paper_ledger_path, calendar=calendar,
        )  # fmt: skip
        return
    dsn = settings.database_url.get_secret_value()
    applied = await asyncio.to_thread(migrate, dsn)
    if applied:
        log.info("applied migrations: %s", ", ".join(applied))
    rt.paper_store = PaperStore(dsn)
    gate = build_gate(rt.configs, calendar=calendar)
    loaded = await asyncio.to_thread(rt.paper_store.load, PAPER_ACCOUNT, gate)
    if loaded is None:
        rt.paper = PaperExecution(
            account_id=PAPER_ACCOUNT, starting_capital=Decimal(str(settings.paper_starting_capital)), gate=gate, opened_at=now
        )
        await rt.paper_service.flush()
    else:
        rt.paper = loaded
        actions = await rt.paper_service.recover(now)
        log.info("paper state restored (%d events); recovery: %s", len(loaded.ledger.events), actions or "none")


def build_paper(
    configs: dict[str, IndexConfig],
    *,
    starting_capital: Decimal,
    now: datetime,
    ledger_path: Path | None = None,
    fill_model: FillModel | None = None,
    risk: RiskConfig | None = None,
    calendar: SessionCalendar | None = None,
    policy: ExecutionPolicy | None = None,
) -> PaperExecution:
    gate = build_gate(configs, fill_model=fill_model, risk=risk, calendar=calendar)
    return PaperExecution(
        account_id=PAPER_ACCOUNT,
        starting_capital=starting_capital,
        gate=gate,
        opened_at=now,
        ledger=Ledger(ledger_path),
        policy=policy,
    )


async def download_master(url: str, timeout_s: float = 90.0) -> InstrumentMaster:
    async with httpx.AsyncClient() as c:
        resp = await c.get(url, timeout=timeout_s)
        resp.raise_for_status()
        return InstrumentMaster.parse(resp.content, datetime.now(UTC))


async def build_runtime(settings: Settings) -> Runtime:
    configs = load_index_configs(settings.strategy_config_dir)
    rt = Runtime(configs=configs)
    await init_paper(rt, settings, datetime.now(UTC))
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
    if settings.dhan_feed_enabled:
        svc = rt.paper_service

        async def on_quote(q):
            await svc.call(lambda e: e.process_market_event(q))

        rt.collector = MarketFeedCollector(settings.dhan_client_id, rt.token.get, on_quote)
    return rt
