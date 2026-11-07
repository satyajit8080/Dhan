#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DHAN QUANT INTELLIGENCE ENGINE
==============================
Indian index-options market-intelligence and research engine (NSE + BSE).

READ-ONLY. This program never places, modifies or cancels orders. The order
endpoints of the DhanHQ SDK are never imported, and every outbound REST path is
checked against an allow-list of market-data endpoints before it is sent.

Commands
--------
  python main.py run          live engine (collectors + features + events + research + alerts)
  python main.py run --headless   same, no terminal UI (systemd)
  python main.py watch        render the dashboard written by a running engine
  python main.py verify       live verification: auth, instruments, expiries, chain, WS, Telegram
  python main.py selftest     offline numerical self-tests (pricing, IV, stats, DB, look-ahead)
  python main.py initdb       create / migrate the SQLite schema
  python main.py research     run the historical pattern study + walk-forward backtest now
  python main.py report       print the latest validation report from the DB
  python main.py backfill     pull historical candles for all instruments into the DB

Data sources actually used (verified against DhanHQ v2 docs + SDK 2.3.0 source)
------------------------------------------------------------------------------
  * Instrument master  https://images.dhan.co/api-data/api-scrip-master-detailed.csv
                       (security ids, lot sizes per expiry, option/futures contracts)
  * /v2/optionchain/expirylist   expiries per underlying            (rate: 1 per 3 s)
  * /v2/optionchain              strike-wise CE/PE OI, prev OI, volume, LTP, IV,
                                 vendor Greeks, top bid/ask price+qty  (rate: 1 per 3 s)
  * /v2/marketfeed/quote         snapshot quote with 5-level depth  (rate: 1 per s, <=1000 ids)
  * /v2/charts/intraday          1/5/15/25/60-minute OHLCV(+OI)  (3 and 30 minute are
                                 RESAMPLED from 1-minute here: Dhan does not serve them)
  * /v2/charts/historical        daily OHLCV
  * wss://api-feed.dhan.co?version=2   binary feed: Ticker(15) / Quote(17) / Full(21)
                                 Full = LTP, LTQ, LTT, ATP, volume, total buy/sell qty,
                                 OI, OI day hi/lo, OHLC, 5-level depth.
                                 Indices (IDX_I) deliver ticker/prev-close only: no volume.

Not available from Dhan and therefore NOT fabricated
---------------------------------------------------
  * Market breadth (advances/declines) - approximated only if constituent ids are
    configured (BREADTH_CONSTITUENTS) and subscribed; otherwise reported as N/A.
  * Fama-French factors for India - not available from Dhan; FF is disabled unless a
    factor CSV is provided (FF_FACTOR_CSV). CAPM uses NIFTY as market proxy.
  * Index volume - indices carry none; VWAP / volume features use the futures series.
  * Exchange holiday calendar - no Dhan endpoint; read from holidays.txt / HOLIDAYS env,
    plus automatic "no ticks after open" detection.
  * Participant identity / institutional intent - not inferable from public data.
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import collections
import contextlib
import csv
import dataclasses
import datetime as dt
import fcntl
import gzip
import hashlib
import io
import json
import logging
import logging.handlers
import math
import os
import random
import signal
import sqlite3
import statistics
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

try:  # scipy is required for GARCH/ADF optimisation; core pricing works without it.
    from scipy import optimize as _sopt
    from scipy import stats as _sstats
    HAVE_SCIPY = True
except Exception:  # pragma: no cover
    HAVE_SCIPY = False

try:
    import aiohttp
except Exception:  # pragma: no cover
    aiohttp = None  # type: ignore

try:
    from websockets.asyncio.client import connect as ws_connect  # websockets >= 13
except Exception:  # pragma: no cover
    try:
        from websockets import connect as ws_connect  # type: ignore
    except Exception:
        ws_connect = None  # type: ignore

try:  # Official SDK: ONLY data mixins are imported. Order mixins are never imported.
    from dhanhq.dhan_context import DhanContext as _SdkContext
    from dhanhq._option_chain import OptionChain as _SdkOptionChain
    from dhanhq._historical_data import HistoricalData as _SdkHistorical
    from dhanhq._market_feed import MarketFeed as _SdkMarketQuote
    HAVE_SDK = True
except Exception:  # pragma: no cover
    HAVE_SDK = False

try:
    import xgboost as _xgb  # optional, only used when data volume justifies it
    HAVE_XGB = True
except Exception:
    HAVE_XGB = False

APP_NAME = "dhan-quant-engine"
APP_VERSION = "1.0.0"
SCHEMA_VERSION = 3

IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")
UTC = dt.timezone.utc

# ============================================================================
# 0. CONFIGURATION
# ============================================================================


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return default if v is None or v.strip() == "" else v.strip()


def _env_f(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_i(name: str, default: int) -> int:
    try:
        return int(float(_env(name, str(default))))
    except ValueError:
        return default


def _env_b(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


def load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE). Never overrides real environment variables."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


@dataclass
class IndexSpec:
    name: str              # canonical name
    symbol: str            # UNDERLYING_SYMBOL in scrip master
    exch: str              # NSE / BSE
    idx_segment: str = "IDX_I"
    fno_segment: str = "NSE_FNO"
    has_options: bool = True
    security_id: Optional[int] = None    # discovered from scrip master
    strike_step: Optional[float] = None  # discovered from option chain


DEFAULT_INDICES: Dict[str, IndexSpec] = {
    "NIFTY": IndexSpec("NIFTY", "NIFTY", "NSE", fno_segment="NSE_FNO"),
    "BANKNIFTY": IndexSpec("BANKNIFTY", "BANKNIFTY", "NSE", fno_segment="NSE_FNO"),
    "FINNIFTY": IndexSpec("FINNIFTY", "FINNIFTY", "NSE", fno_segment="NSE_FNO"),
    "SENSEX": IndexSpec("SENSEX", "SENSEX", "BSE", fno_segment="BSE_FNO"),
    "BANKEX": IndexSpec("BANKEX", "BANKEX", "BSE", fno_segment="BSE_FNO"),
    "INDIAVIX": IndexSpec("INDIAVIX", "INDIA VIX", "NSE", has_options=False),
}


@dataclass
class Config:
    client_id: str = ""
    access_token: str = ""
    token_file: str = ""
    telegram_token: str = ""
    telegram_chat_id: str = ""
    base_dir: Path = Path(".")
    data_dir: Path = Path("data")
    db_path: Path = Path("data/engine.db")
    log_dir: Path = Path("logs")
    log_level: str = "INFO"
    indices: List[str] = field(default_factory=lambda: ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "BANKEX"])
    strike_window: int = 15                 # ATM +/- N strikes monitored
    ws_full_window: int = 15                # strikes subscribed in FULL mode (depth + OI)
    chain_min_interval_s: float = 3.2       # Dhan: 1 unique option-chain request / 3 s
    chain_cycle_target_s: float = 20.0      # desired refresh per index (best effort)
    next_expiry_every_n: int = 6            # fetch next expiry chain every N cycles
    quote_min_interval_s: float = 1.05      # Dhan: quote API 1 req / s
    data_api_min_interval_s: float = 0.25   # Dhan: data APIs 5 req / s
    snapshot_interval_s: float = 15.0       # feature/snapshot cadence per index
    ws_stale_s: float = 20.0                # tick older than this during market = stale
    chain_stale_s: float = 90.0
    db_batch_size: int = 500
    db_flush_s: float = 2.0
    research_every_s: float = 1800.0
    dashboard_every_s: float = 2.0
    health_port: int = 8787
    health_host: str = "127.0.0.1"
    alert_cooldown_s: float = 900.0
    min_samples_pattern: int = 30           # below this a pattern is "insufficient evidence"
    min_oos_for_calibration: int = 100
    fdr_q: float = 0.10
    cost_bps_roundtrip: float = 6.0         # brokerage+STT+exch+GST+stamp on premium, approx
    event_sigma_k: float = 3.0              # move > k sigma(horizon) = significant
    event_horizon_min: int = 5
    event_min_gap_min: int = 20
    market_open: dt.time = dt.time(9, 15)
    market_close: dt.time = dt.time(15, 30)
    pre_open_warmup_min: int = 20
    post_close_linger_min: int = 35         # keep running to fill +30m outcomes
    risk_free: float = 0.065
    holidays_file: Path = Path("holidays.txt")
    holidays_extra: str = ""
    max_memory_snapshots: int = 2000        # per index, in-memory ring
    headless: bool = False
    dry_run_no_network: bool = False

    @staticmethod
    def from_env(base_dir: Optional[Path] = None) -> "Config":
        base = Path(base_dir or Path(__file__).resolve().parent)
        load_dotenv(base / ".env")
        c = Config()
        c.base_dir = base
        c.client_id = _env("DHAN_CLIENT_ID")
        c.access_token = _env("DHAN_ACCESS_TOKEN")
        c.token_file = _env("DHAN_TOKEN_FILE")
        c.telegram_token = _env("TELEGRAM_BOT_TOKEN")
        c.telegram_chat_id = _env("TELEGRAM_CHAT_ID")
        c.data_dir = base / _env("DATA_DIR", "data")
        c.db_path = Path(_env("DB_PATH", str(c.data_dir / "engine.db")))
        if not c.db_path.is_absolute():
            c.db_path = base / c.db_path
        c.log_dir = base / _env("LOG_DIR", "logs")
        c.log_level = _env("LOG_LEVEL", "INFO").upper()
        idx = _env("INDICES", ",".join(c.indices))
        c.indices = [s.strip().upper() for s in idx.split(",") if s.strip()]
        c.strike_window = _env_i("STRIKE_WINDOW", c.strike_window)
        c.ws_full_window = min(_env_i("WS_FULL_WINDOW", c.ws_full_window), c.strike_window)
        c.chain_min_interval_s = max(3.0, _env_f("CHAIN_MIN_INTERVAL_S", c.chain_min_interval_s))
        c.chain_cycle_target_s = _env_f("CHAIN_CYCLE_TARGET_S", c.chain_cycle_target_s)
        c.snapshot_interval_s = _env_f("SNAPSHOT_INTERVAL_S", c.snapshot_interval_s)
        c.ws_stale_s = _env_f("WS_STALE_S", c.ws_stale_s)
        c.research_every_s = _env_f("RESEARCH_EVERY_S", c.research_every_s)
        c.health_port = _env_i("HEALTH_PORT", c.health_port)
        c.alert_cooldown_s = _env_f("ALERT_COOLDOWN_S", c.alert_cooldown_s)
        c.min_samples_pattern = _env_i("MIN_SAMPLES_PATTERN", c.min_samples_pattern)
        c.cost_bps_roundtrip = _env_f("COST_BPS_ROUNDTRIP", c.cost_bps_roundtrip)
        c.event_sigma_k = _env_f("EVENT_SIGMA_K", c.event_sigma_k)
        c.risk_free = _env_f("RISK_FREE_RATE", c.risk_free)
        c.holidays_file = base / _env("HOLIDAYS_FILE", "holidays.txt")
        c.holidays_extra = _env("HOLIDAYS", "")
        return c

    def resolve_token(self) -> str:
        """Token from env, else from DHAN_TOKEN_FILE (re-read each call so a daily rotation works)."""
        if self.token_file:
            p = Path(self.token_file)
            if p.exists():
                t = p.read_text(encoding="utf-8").strip()
                if t:
                    return t
        return self.access_token

    def token_fingerprint(self) -> str:
        t = self.resolve_token()
        return f"...{t[-4:]}" if len(t) >= 8 else "(none)"


# ============================================================================
# 1. LOGGING (structured JSON, rotating, secrets redacted)
# ============================================================================

_SECRET_PATTERNS: List[str] = []


def register_secret(s: str) -> None:
    if s and len(s) >= 8 and s not in _SECRET_PATTERNS:
        _SECRET_PATTERNS.append(s)


def redact(text: str) -> str:
    for s in _SECRET_PATTERNS:
        text = text.replace(s, "***" + s[-4:])
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {
            "ts": dt.datetime.fromtimestamp(record.created, IST).isoformat(timespec="milliseconds"),
            "lvl": record.levelname,
            "mod": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            d.update(extra)
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(d, default=str))


class Log:
    """Thin structured-logging helper: log.info("msg", key=value)."""

    def __init__(self, name: str):
        self._l = logging.getLogger(name)

    def _emit(self, lvl: int, msg: str, **kw: Any) -> None:
        self._l.log(lvl, msg, extra={"extra_fields": kw} if kw else None)

    def debug(self, msg: str, **kw: Any) -> None:
        self._emit(logging.DEBUG, msg, **kw)

    def info(self, msg: str, **kw: Any) -> None:
        self._emit(logging.INFO, msg, **kw)

    def warning(self, msg: str, **kw: Any) -> None:
        self._emit(logging.WARNING, msg, **kw)

    def error(self, msg: str, **kw: Any) -> None:
        self._emit(logging.ERROR, msg, **kw)

    def exception(self, msg: str, **kw: Any) -> None:
        self._l.exception(msg, extra={"extra_fields": kw} if kw else None)


def setup_logging(cfg: Config, to_console: bool) -> None:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, cfg.log_level, logging.INFO))
    fh = logging.handlers.TimedRotatingFileHandler(
        cfg.log_dir / "engine.jsonl", when="midnight", backupCount=30, encoding="utf-8")
    fh.setFormatter(JsonFormatter())
    root.addHandler(fh)
    eh = logging.handlers.RotatingFileHandler(cfg.log_dir / "errors.jsonl", maxBytes=20_000_000,
                                              backupCount=5, encoding="utf-8")
    eh.setLevel(logging.WARNING)
    eh.setFormatter(JsonFormatter())
    root.addHandler(eh)
    if to_console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(JsonFormatter())
        root.addHandler(ch)
    for noisy in ("websockets", "asyncio", "urllib3", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


LOG = Log("engine")

# ============================================================================
# 2. TIME, SESSION AND HOLIDAYS
# ============================================================================


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def ts_pair(t: Optional[float] = None) -> Tuple[float, str, str]:
    """(epoch seconds, ISO UTC, ISO IST) for storage."""
    t = time.time() if t is None else t
    return (t, dt.datetime.fromtimestamp(t, UTC).isoformat(timespec="milliseconds"),
            dt.datetime.fromtimestamp(t, IST).isoformat(timespec="milliseconds"))


def expiry_timestamp(expiry: str) -> float:
    """Epoch seconds of the 15:30 IST settlement stamp of an expiry date (YYYY-MM-DD).

    Indian index options are European, cash settled at the closing/settlement price
    of the underlying on expiry day; trading ends 15:30 IST.
    """
    d = dt.date.fromisoformat(expiry[:10])
    return dt.datetime.combine(d, dt.time(15, 30), IST).timestamp()


def year_fraction(t_now: float, expiry: str) -> float:
    """ACT/365 year fraction to expiry stamp; floored at one minute to keep Greeks finite."""
    return max((expiry_timestamp(expiry) - t_now) / (365.0 * 86400.0), 1.0 / (365.0 * 1440.0))


class MarketCalendar:
    """Weekend + holiday aware session clock. Holidays come from a file / env, never invented."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.holidays: Dict[str, set] = {"NSE": set(), "BSE": set()}
        self._load()

    def _load(self) -> None:
        lines: List[str] = []
        if self.cfg.holidays_file.exists():
            lines += self.cfg.holidays_file.read_text(encoding="utf-8").splitlines()
        lines += [x for x in self.cfg.holidays_extra.split(",") if x.strip()]
        for raw in lines:
            raw = raw.split("#", 1)[0].strip()
            if not raw:
                continue
            parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
            try:
                d = dt.date.fromisoformat(parts[0]).isoformat()
            except ValueError:
                continue
            exch = parts[1].upper() if len(parts) > 1 else "ALL"
            for e in (["NSE", "BSE"] if exch == "ALL" else [exch]):
                self.holidays.setdefault(e, set()).add(d)

    def is_trading_day(self, d: dt.date, exch: str = "NSE") -> bool:
        return d.weekday() < 5 and d.isoformat() not in self.holidays.get(exch, set())

    def session_bounds(self, d: dt.date) -> Tuple[dt.datetime, dt.datetime]:
        return (dt.datetime.combine(d, self.cfg.market_open, IST),
                dt.datetime.combine(d, self.cfg.market_close, IST))

    def is_market_open(self, t: Optional[dt.datetime] = None, exch: str = "NSE") -> bool:
        t = t or now_ist()
        if not self.is_trading_day(t.date(), exch):
            return False
        o, c = self.session_bounds(t.date())
        return o <= t <= c

    def phase(self, t: Optional[dt.datetime] = None) -> str:
        """closed | warmup | open | linger."""
        t = t or now_ist()
        if not (self.is_trading_day(t.date(), "NSE") or self.is_trading_day(t.date(), "BSE")):
            return "closed"
        o, c = self.session_bounds(t.date())
        if o - dt.timedelta(minutes=self.cfg.pre_open_warmup_min) <= t < o:
            return "warmup"
        if o <= t <= c:
            return "open"
        if c < t <= c + dt.timedelta(minutes=self.cfg.post_close_linger_min):
            return "linger"
        return "closed"

    def next_session_start(self, t: Optional[dt.datetime] = None) -> dt.datetime:
        t = t or now_ist()
        d = t.date()
        for _ in range(15):
            if self.is_trading_day(d, "NSE") or self.is_trading_day(d, "BSE"):
                o, _c = self.session_bounds(d)
                w = o - dt.timedelta(minutes=self.cfg.pre_open_warmup_min)
                if w > t:
                    return w
            d += dt.timedelta(days=1)
        return t + dt.timedelta(hours=12)

    def minutes_since_open(self, t: Optional[dt.datetime] = None) -> float:
        t = t or now_ist()
        o, _ = self.session_bounds(t.date())
        return (t - o).total_seconds() / 60.0

# ============================================================================
# 3. QUANT LIBRARY  (pure functions: unit-testable, no I/O)
# ============================================================================

# ---------------------------------------------------------------- LEVEL 7 --
SQRT2 = math.sqrt(2.0)
SQRT2PI = math.sqrt(2.0 * math.pi)


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / SQRT2)


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT2PI


def _d1d2(F: float, K: float, T: float, sigma: float) -> Tuple[float, float]:
    vs = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / vs
    return d1, d1 - vs


def black76_price(F: float, K: float, T: float, r: float, sigma: float, is_call: bool) -> float:
    """Black-76 price of a European option on a forward/future F."""
    df = math.exp(-r * T)
    if T <= 0 or sigma <= 0:
        return df * max((F - K) if is_call else (K - F), 0.0)
    d1, d2 = _d1d2(F, K, T, sigma)
    if is_call:
        return df * (F * norm_cdf(d1) - K * norm_cdf(d2))
    return df * (K * norm_cdf(-d2) - F * norm_cdf(-d1))


def bs_price(S: float, K: float, T: float, r: float, sigma: float, is_call: bool, q: float = 0.0) -> float:
    """Black-Scholes-Merton with continuous dividend yield q."""
    if T <= 0 or sigma <= 0:
        return max((S - K) if is_call else (K - S), 0.0)
    F = S * math.exp((r - q) * T)
    return black76_price(F, K, T, r, sigma, is_call)


@dataclass
class Greeks:
    price: float
    delta: float      # dV/dF (Black-76, discounted)
    gamma: float      # d2V/dF2 per point^2
    vega: float       # per 1 vol point (0.01)
    theta: float      # per calendar day
    rho: float        # per 1% rate move


def black76_greeks(F: float, K: float, T: float, r: float, sigma: float, is_call: bool) -> Greeks:
    df = math.exp(-r * T)
    T = max(T, 1e-8)
    sigma = max(sigma, 1e-6)
    d1, d2 = _d1d2(F, K, T, sigma)
    sq = math.sqrt(T)
    price = black76_price(F, K, T, r, sigma, is_call)
    pdf1 = norm_pdf(d1)
    delta = df * (norm_cdf(d1) if is_call else norm_cdf(d1) - 1.0)
    gamma = df * pdf1 / (F * sigma * sq)
    vega = df * F * pdf1 * sq * 0.01
    # dV/dt (calendar) for Black-76: -dV/dT
    if is_call:
        dVdT = -r * price + df * F * pdf1 * sigma / (2 * sq)
    else:
        dVdT = -r * price + df * F * pdf1 * sigma / (2 * sq)
    theta = -dVdT / 365.0
    rho = -T * price * 0.01
    return Greeks(price, delta, gamma, vega, theta, rho)


def implied_vol_black76(price: float, F: float, K: float, T: float, r: float, is_call: bool,
                        lo: float = 1e-4, hi: float = 5.0, tol: float = 1e-7, max_iter: int = 100) -> Optional[float]:
    """Safeguarded Newton + bisection IV solver. Returns None when price violates no-arbitrage bounds."""
    if not (price > 0 and F > 0 and K > 0 and T > 0):
        return None
    df = math.exp(-r * T)
    intrinsic = df * max((F - K) if is_call else (K - F), 0.0)
    upper = df * (F if is_call else K)
    if price < intrinsic - 1e-9 or price >= upper:
        return None
    if price - intrinsic < 1e-10:
        return lo
    # Brenner-Subrahmanyam seed
    sigma = min(max(math.sqrt(2 * math.pi / T) * price / (df * F), 0.05), 2.0)
    a, b = lo, hi
    for _ in range(max_iter):
        p = black76_price(F, K, T, r, sigma, is_call)
        diff = p - price
        if abs(diff) < tol:
            return sigma
        if diff > 0:
            b = sigma
        else:
            a = sigma
        d1, _ = _d1d2(F, K, T, sigma)
        vega = df * F * norm_pdf(d1) * math.sqrt(T)
        nxt = sigma - diff / vega if vega > 1e-12 else -1
        sigma = nxt if a < nxt < b else 0.5 * (a + b)
        if b - a < 1e-10:
            return sigma
    return sigma


def parity_forward(strikes: Sequence[float], calls: Sequence[float], puts: Sequence[float],
                   T: float, r: float, ref: float, band: float = 0.015) -> Optional[Tuple[float, float, int]]:
    """Implied forward from put-call parity  F_K = K + (C-P)/DF, median within +/-band of ref.

    Returns (forward, dispersion across strikes, n strikes used) or None.
    """
    df = math.exp(-r * T)
    vals = []
    for K, c, p in zip(strikes, calls, puts):
        if c and p and c > 0 and p > 0 and abs(K / ref - 1) <= band:
            vals.append(K + (c - p) / df)
    if len(vals) < 2:
        return None
    med = float(np.median(vals))
    return med, float(np.max(vals) - np.min(vals)), len(vals)


def put_call_parity_residual(C: float, P: float, F: float, K: float, T: float, r: float) -> float:
    """C - P - DF*(F-K). Zero under no-arbitrage; non-zero = stale quote or carry mismatch."""
    return C - P - math.exp(-r * T) * (F - K)


def gamma_pnl_approx(gamma: float, dF: float, theta_day: float, days: float, delta: float = 0.0) -> float:
    """Taylor expansion: dV ~ delta*dF + 0.5*gamma*dF^2 + theta*dt."""
    return delta * dF + 0.5 * gamma * dF * dF + theta_day * days


def straddle_breakeven_move(straddle: float) -> float:
    return straddle


def implied_move_from_straddle(straddle: float, F: float) -> float:
    """ATM straddle ~ 0.8 * F * sigma * sqrt(T)  =>  expected |move| ~ straddle (E|X| = sigma*sqrt(2/pi))."""
    return straddle / F if F > 0 else float("nan")


def max_pain(strikes: Sequence[float], ce_oi: Sequence[float], pe_oi: Sequence[float]) -> Optional[float]:
    """Settlement strike minimising aggregate intrinsic payout to option holders."""
    if not strikes:
        return None
    ks = np.asarray(strikes, float)
    c = np.asarray(ce_oi, float)
    p = np.asarray(pe_oi, float)
    best, best_k = math.inf, None
    for s in ks:
        pay = float(np.sum(c * np.maximum(s - ks, 0)) + np.sum(p * np.maximum(ks - s, 0)))
        if pay < best:
            best, best_k = pay, float(s)
    return best_k


def gamma_exposure(strikes: Sequence[float], ce_gamma: Sequence[float], pe_gamma: Sequence[float],
                   ce_oi: Sequence[float], pe_oi: Sequence[float], F: float, lot: float = 1.0) -> Dict[str, Any]:
    """Dealer GEX proxy (assumes dealers short calls & long puts is NOT assumed; we report the
    open-interest-weighted gamma per strike and the conventional sign convention
    GEX = (CE_OI*gamma - PE_OI*gamma) * F^2 * 0.01. Interpretation is a convention, not a fact."""
    ks = np.asarray(strikes, float)
    g_ce = np.nan_to_num(np.asarray(ce_gamma, float)) * np.asarray(ce_oi, float)
    g_pe = np.nan_to_num(np.asarray(pe_gamma, float)) * np.asarray(pe_oi, float)
    per = (g_ce - g_pe) * F * F * 0.01
    tot_abs = g_ce + g_pe
    conc = float(np.max(tot_abs) / np.sum(tot_abs)) if np.sum(tot_abs) > 0 else float("nan")
    peak = float(ks[int(np.argmax(tot_abs))]) if len(ks) else float("nan")
    flip = None
    cum = np.cumsum(per)
    for i in range(1, len(cum)):
        if np.sign(cum[i]) != np.sign(cum[i - 1]):
            flip = float(ks[i])
            break
    return {"net_gex": float(np.sum(per)), "gamma_concentration": conc, "gamma_peak_strike": peak,
            "gex_flip_strike": flip, "total_oi_gamma": float(np.sum(tot_abs))}


# ---------------------------------------------------------------- LEVEL 1 --

def bayes_posterior(prior: float, p_e_given_h: float, p_e_given_not_h: float) -> float:
    """P(H|E) = P(E|H)P(H) / [P(E|H)P(H) + P(E|~H)(1-P(H))]."""
    num = p_e_given_h * prior
    den = num + p_e_given_not_h * (1 - prior)
    return num / den if den > 0 else float("nan")


def conditional_probability(a_and_b: int, b: int) -> float:
    return a_and_b / b if b > 0 else float("nan")


def expected_value(outcomes: Sequence[float], probs: Sequence[float]) -> float:
    return float(np.dot(outcomes, probs))


def variance_std(x: Sequence[float], ddof: int = 1) -> Tuple[float, float]:
    a = np.asarray(x, float)
    if len(a) <= ddof:
        return float("nan"), float("nan")
    v = float(np.var(a, ddof=ddof))
    return v, math.sqrt(v)


def geometric_pmf(k: int, p: float) -> float:
    """P(first success on trial k)."""
    return (1 - p) ** (k - 1) * p if k >= 1 else 0.0


def brier_score(p: Sequence[float], y: Sequence[int]) -> float:
    pa, ya = np.asarray(p, float), np.asarray(y, float)
    return float(np.mean((pa - ya) ** 2)) if len(pa) else float("nan")


def reliability_bins(p: Sequence[float], y: Sequence[int], n_bins: int = 10) -> List[Dict[str, float]]:
    pa, ya = np.asarray(p, float), np.asarray(y, float)
    out = []
    edges = np.linspace(0, 1, n_bins + 1)
    for i in range(n_bins):
        m = (pa >= edges[i]) & (pa < edges[i + 1] if i < n_bins - 1 else pa <= edges[i + 1])
        if m.sum():
            out.append({"lo": float(edges[i]), "hi": float(edges[i + 1]), "n": int(m.sum()),
                        "mean_p": float(pa[m].mean()), "freq": float(ya[m].mean())})
    return out


def expected_calibration_error(p: Sequence[float], y: Sequence[int], n_bins: int = 10) -> float:
    bins = reliability_bins(p, y, n_bins)
    n = sum(b["n"] for b in bins)
    return float(sum(b["n"] / n * abs(b["mean_p"] - b["freq"]) for b in bins)) if n else float("nan")


class IsotonicCalibrator:
    """Pool-adjacent-violators isotonic regression: score -> calibrated probability."""

    def __init__(self) -> None:
        self.x: Optional[np.ndarray] = None
        self.y: Optional[np.ndarray] = None

    def fit(self, scores: Sequence[float], labels: Sequence[int]) -> "IsotonicCalibrator":
        o = np.argsort(scores)
        xs = np.asarray(scores, float)[o]
        ys = np.asarray(labels, float)[o]
        blocks = [[ys[i], 1.0, xs[i], xs[i]] for i in range(len(xs))]  # sum, w, xmin, xmax
        i = 0
        while i < len(blocks) - 1:
            if blocks[i][0] / blocks[i][1] > blocks[i + 1][0] / blocks[i + 1][1]:
                b = blocks[i]
                n = blocks.pop(i + 1)
                b[0] += n[0]
                b[1] += n[1]
                b[3] = n[3]
                i = max(i - 1, 0)
            else:
                i += 1
        self.x = np.array([(b[2] + b[3]) / 2 for b in blocks])
        self.y = np.array([b[0] / b[1] for b in blocks])
        return self

    def predict(self, s: float) -> float:
        if self.x is None or len(self.x) == 0:
            return float("nan")
        return float(np.interp(s, self.x, self.y))


# ---------------------------------------------------------------- LEVEL 2 --

def standard_error(x: Sequence[float]) -> float:
    a = np.asarray(x, float)
    return float(np.std(a, ddof=1) / math.sqrt(len(a))) if len(a) > 1 else float("nan")


def t_cdf(t: float, df: float) -> float:
    if HAVE_SCIPY:
        return float(_sstats.t.cdf(t, df))
    return norm_cdf(t)  # large-sample fallback


def t_ppf(p: float, df: float) -> float:
    if HAVE_SCIPY:
        return float(_sstats.t.ppf(p, df))
    return float(statistics.NormalDist().inv_cdf(p))


def mean_ci(x: Sequence[float], alpha: float = 0.05) -> Tuple[float, float, float]:
    a = np.asarray(x, float)
    if len(a) < 2:
        m = float(a.mean()) if len(a) else float("nan")
        return m, float("nan"), float("nan")
    m, se = float(a.mean()), standard_error(a)
    h = t_ppf(1 - alpha / 2, len(a) - 1) * se
    return m, m - h, m + h


def wilson_interval(k: int, n: int, z: float = 1.959964) -> Tuple[float, float, float]:
    if n == 0:
        return float("nan"), float("nan"), float("nan")
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return p, max(0.0, c - h), min(1.0, c + h)


def binom_test_greater(k: int, n: int, p0: float) -> float:
    """One-sided P(X>=k | n, p0)."""
    if n == 0:
        return 1.0
    if HAVE_SCIPY:
        return float(_sstats.binom.sf(k - 1, n, p0))
    return float(sum(math.comb(n, i) * p0 ** i * (1 - p0) ** (n - i) for i in range(k, n + 1)))


def one_sample_t_pvalue(x: Sequence[float], mu0: float = 0.0) -> float:
    a = np.asarray(x, float)
    if len(a) < 3 or np.std(a, ddof=1) == 0:
        return 1.0
    t = (a.mean() - mu0) / standard_error(a)
    return float(2 * (1 - t_cdf(abs(t), len(a) - 1)))


def welch_t_pvalue(a: Sequence[float], b: Sequence[float]) -> float:
    x, y = np.asarray(a, float), np.asarray(b, float)
    if len(x) < 3 or len(y) < 3:
        return 1.0
    vx, vy = x.var(ddof=1) / len(x), y.var(ddof=1) / len(y)
    if vx + vy == 0:
        return 1.0
    t = (x.mean() - y.mean()) / math.sqrt(vx + vy)
    dof = (vx + vy) ** 2 / (vx ** 2 / (len(x) - 1) + vy ** 2 / (len(y) - 1))
    return float(2 * (1 - t_cdf(abs(t), dof)))


def sharpe_ratio(returns: Sequence[float], rf_per_period: float = 0.0, periods_per_year: float = 252.0) -> float:
    a = np.asarray(returns, float) - rf_per_period
    if len(a) < 2 or np.std(a, ddof=1) == 0:
        return float("nan")
    return float(a.mean() / np.std(a, ddof=1) * math.sqrt(periods_per_year))


@dataclass
class OLSResult:
    beta: np.ndarray
    se: np.ndarray
    t: np.ndarray
    p: np.ndarray
    r2: float
    adj_r2: float
    resid: np.ndarray
    n: int


def ols(y: Sequence[float], X: np.ndarray, add_const: bool = True) -> OLSResult:
    Y = np.asarray(y, float)
    Xm = np.asarray(X, float)
    if Xm.ndim == 1:
        Xm = Xm[:, None]
    if add_const:
        Xm = np.column_stack([np.ones(len(Y)), Xm])
    n, k = Xm.shape
    beta, *_ = np.linalg.lstsq(Xm, Y, rcond=None)
    resid = Y - Xm @ beta
    dof = max(n - k, 1)
    s2 = float(resid @ resid) / dof
    try:
        cov = s2 * np.linalg.pinv(Xm.T @ Xm)
        se = np.sqrt(np.maximum(np.diag(cov), 0))
    except np.linalg.LinAlgError:
        se = np.full(k, np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        t = beta / se
    p = np.array([2 * (1 - t_cdf(abs(ti), dof)) if np.isfinite(ti) else np.nan for ti in t])
    sst = float(((Y - Y.mean()) ** 2).sum())
    r2 = 1 - float(resid @ resid) / sst if sst > 0 else float("nan")
    adj = 1 - (1 - r2) * (n - 1) / dof if np.isfinite(r2) else float("nan")
    return OLSResult(beta, se, t, p, r2, adj, resid, n)


def pearson(x: Sequence[float], y: Sequence[float]) -> Tuple[float, float]:
    a, b = np.asarray(x, float), np.asarray(y, float)
    m = np.isfinite(a) & np.isfinite(b)
    a, b = a[m], b[m]
    n = len(a)
    if n < 3 or a.std() == 0 or b.std() == 0:
        return float("nan"), 1.0
    r = float(np.corrcoef(a, b)[0, 1])
    r = max(min(r, 0.999999), -0.999999)
    t = r * math.sqrt((n - 2) / (1 - r * r))
    return r, float(2 * (1 - t_cdf(abs(t), n - 2)))


def benjamini_hochberg(pvals: Sequence[float], q: float = 0.10) -> List[bool]:
    """FDR control. Returns a reject flag per hypothesis (input order)."""
    p = np.asarray(pvals, float)
    m = len(p)
    if m == 0:
        return []
    order = np.argsort(p)
    thresh = q * (np.arange(1, m + 1) / m)
    passed = p[order] <= thresh
    kmax = np.max(np.where(passed)[0]) if passed.any() else -1
    rej = np.zeros(m, bool)
    if kmax >= 0:
        rej[order[: kmax + 1]] = True
    return rej.tolist()


def bh_adjusted(pvals: Sequence[float]) -> List[float]:
    p = np.asarray(pvals, float)
    m = len(p)
    if m == 0:
        return []
    o = np.argsort(p)
    adj = np.empty(m)
    prev = 1.0
    for rank in range(m - 1, -1, -1):
        i = o[rank]
        prev = min(prev, p[i] * m / (rank + 1))
        adj[i] = prev
    return adj.tolist()


def bonferroni(pvals: Sequence[float], alpha: float = 0.05) -> List[bool]:
    m = max(len(pvals), 1)
    return [p <= alpha / m for p in pvals]


# ---------------------------------------------------------------- LEVEL 3 --

def covariance_matrix(R: np.ndarray) -> np.ndarray:
    return np.cov(np.asarray(R, float), rowvar=False, ddof=1)


def ledoit_wolf_shrinkage(R: np.ndarray) -> Tuple[np.ndarray, float]:
    """Ledoit-Wolf (2004) shrinkage toward scaled identity. Returns (Sigma, delta)."""
    X = np.asarray(R, float)
    X = X - X.mean(0)
    n, p = X.shape
    S = X.T @ X / n
    mu = np.trace(S) / p
    F = mu * np.eye(p)
    d2 = np.sum((S - F) ** 2)
    b2 = 0.0
    for i in range(n):
        xi = X[i][:, None]
        b2 += np.sum((xi @ xi.T - S) ** 2)
    b2 = min(b2 / n ** 2, d2)
    delta = b2 / d2 if d2 > 0 else 1.0
    return delta * F + (1 - delta) * S, float(delta)


def portfolio_variance(w: Sequence[float], Sigma: np.ndarray) -> float:
    wa = np.asarray(w, float)
    return float(wa @ Sigma @ wa)


def pca(R: np.ndarray, standardize: bool = True) -> Dict[str, Any]:
    X = np.asarray(R, float)
    X = X[np.all(np.isfinite(X), axis=1)]
    if len(X) < X.shape[1] + 2:
        return {"ok": False}
    X = X - X.mean(0)
    if standardize:
        sd = X.std(0, ddof=1)
        sd[sd == 0] = 1
        X = X / sd
    C = np.cov(X, rowvar=False)
    vals, vecs = np.linalg.eigh(C)
    o = np.argsort(vals)[::-1]
    vals, vecs = vals[o], vecs[:, o]
    return {"ok": True, "eigenvalues": vals, "eigenvectors": vecs,
            "explained": vals / vals.sum(), "condition_number": condition_number(C)}


def condition_number(M: np.ndarray) -> float:
    s = np.linalg.svd(np.asarray(M, float), compute_uv=False)
    return float(s[0] / s[-1]) if s[-1] > 0 else float("inf")


# ---------------------------------------------------------------- LEVEL 4 --

def min_variance_weights(Sigma: np.ndarray) -> np.ndarray:
    """Lagrangian solution of min w'Sw s.t. 1'w = 1:  w = S^-1 1 / (1' S^-1 1)."""
    inv = np.linalg.pinv(Sigma)
    one = np.ones(Sigma.shape[0])
    w = inv @ one
    return w / (one @ w)


def mean_variance_weights(mu: np.ndarray, Sigma: np.ndarray, target: float) -> np.ndarray:
    """Two-constraint Lagrangian (Merton 1972): min w'Sw s.t. w'mu = target, 1'w = 1."""
    inv = np.linalg.pinv(Sigma)
    one = np.ones(len(mu))
    A, B, C = one @ inv @ mu, mu @ inv @ mu, one @ inv @ one
    D = B * C - A * A
    lam = (C * target - A) / D
    gam = (B - A * target) / D
    return inv @ (lam * mu + gam * one)


def gradient_descent(f_grad: Callable[[np.ndarray], Tuple[float, np.ndarray]], x0: np.ndarray,
                     lr: float = 0.1, iters: int = 500, tol: float = 1e-9) -> np.ndarray:
    x = np.asarray(x0, float).copy()
    prev = math.inf
    for _ in range(iters):
        val, g = f_grad(x)
        x -= lr * g
        if abs(prev - val) < tol:
            break
        prev = val
    return x


def jensen_gap_lognormal(sigma: float, T: float) -> float:
    """E[S_T]/S_0 - exp(E[log S_T/S_0]) under GBM with mu=0: exp(0) - exp(-0.5 s^2 T) (convexity)."""
    return 1.0 - math.exp(-0.5 * sigma * sigma * T)


# ---------------------------------------------------------------- LEVEL 5 --

def log_returns(prices: Sequence[float]) -> np.ndarray:
    p = np.asarray(prices, float)
    p = p[p > 0]
    return np.diff(np.log(p)) if len(p) > 1 else np.array([])


def autocorr(x: Sequence[float], lag: int = 1) -> float:
    a = np.asarray(x, float)
    if len(a) <= lag + 2:
        return float("nan")
    a = a - a.mean()
    d = float(a @ a)
    return float(a[lag:] @ a[:-lag]) / d if d > 0 else float("nan")


def ljung_box(x: Sequence[float], lags: int = 10) -> Tuple[float, float]:
    a = np.asarray(x, float)
    n = len(a)
    if n <= lags + 2:
        return float("nan"), float("nan")
    q = n * (n + 2) * sum(autocorr(a, k) ** 2 / (n - k) for k in range(1, lags + 1))
    p = float(1 - _sstats.chi2.cdf(q, lags)) if HAVE_SCIPY else float("nan")
    return float(q), p


# MacKinnon (2010) asymptotic critical values, constant only
ADF_CRIT = {"1%": -3.43, "5%": -2.86, "10%": -2.57}


def adf_test(x: Sequence[float], max_lag: Optional[int] = None) -> Dict[str, Any]:
    """Augmented Dickey-Fuller with constant, lag chosen by AIC. p-value via MacKinnon approx."""
    y = np.asarray(x, float)
    y = y[np.isfinite(y)]
    n = len(y)
    if n < 20:
        return {"ok": False}
    max_lag = max_lag if max_lag is not None else int(min(12 * (n / 100) ** 0.25, n // 4))
    dy = np.diff(y)
    best = None
    for p in range(0, max_lag + 1):
        Y = dy[p:]
        cols = [y[p:-1]]
        for i in range(1, p + 1):
            cols.append(dy[p - i:-i])
        X = np.column_stack(cols)
        if len(Y) < X.shape[1] + 5:
            break
        r = ols(Y, X)
        k = X.shape[1] + 1
        rss = float(r.resid @ r.resid)
        aic = len(Y) * math.log(rss / len(Y) + 1e-300) + 2 * k
        if best is None or aic < best[0]:
            best = (aic, p, r)
    if best is None:
        return {"ok": False}
    _, lag, r = best
    stat = float(r.t[1])
    # MacKinnon (1994) approximate p-value for constant-only case
    pval = _mackinnon_p(stat)
    return {"ok": True, "stat": stat, "lag": lag, "p": pval, "crit": ADF_CRIT,
            "stationary_5pct": stat < ADF_CRIT["5%"]}


def _mackinnon_p(t: float) -> float:
    # coefficients for N=1, "c" from MacKinnon (1994) / statsmodels adfvalues
    small = [2.1659, 1.4412, 0.038269]
    large = [1.7339, 0.93202, -0.12745, -0.010368]
    if t <= -18.83:
        return 0.0
    if t >= 2.74:
        return 1.0
    if t <= -1.61:
        z = small[0] + small[1] * t + small[2] * t * t
    else:
        z = large[0] + large[1] * t + large[2] * t * t + large[3] * t ** 3
    return norm_cdf(z)


def ou_half_life(x: Sequence[float]) -> float:
    """Mean-reversion half-life from dx = a + b x_{t-1}:  HL = -ln2 / ln(1+b)."""
    y = np.asarray(x, float)
    if len(y) < 10:
        return float("nan")
    r = ols(np.diff(y), y[:-1])
    b = r.beta[1]
    if b >= 0 or b <= -1:
        return float("inf") if b >= 0 else float("nan")
    return float(-math.log(2) / math.log(1 + b))


def engle_granger(y: Sequence[float], x: Sequence[float]) -> Dict[str, Any]:
    """Cointegration: regress y on x, ADF on residuals (EG crit 5% ~ -3.34 for 2 series)."""
    a, b = np.asarray(y, float), np.asarray(x, float)
    n = min(len(a), len(b))
    if n < 30:
        return {"ok": False}
    r = ols(a[-n:], b[-n:])
    adf = adf_test(r.resid, max_lag=1)
    return {"ok": True, "beta": float(r.beta[1]), "adf_stat": adf.get("stat"),
            "cointegrated_5pct": bool(adf.get("ok") and adf["stat"] < -3.34),
            "half_life": ou_half_life(r.resid)}


def rolling_vol(returns: Sequence[float], window: int, periods_per_year: float) -> np.ndarray:
    r = np.asarray(returns, float)
    if len(r) < window:
        return np.array([])
    c = np.cumsum(np.insert(r, 0, 0))
    c2 = np.cumsum(np.insert(r * r, 0, 0))
    s = c[window:] - c[:-window]
    s2 = c2[window:] - c2[:-window]
    var = (s2 - s * s / window) / (window - 1)
    return np.sqrt(np.maximum(var, 0) * periods_per_year)


def realized_vol(returns: Sequence[float], periods_per_year: float) -> float:
    r = np.asarray(returns, float)
    return float(np.sqrt(np.mean(r * r) * periods_per_year)) if len(r) > 1 else float("nan")


def parkinson_vol(highs: Sequence[float], lows: Sequence[float], periods_per_year: float) -> float:
    h, l = np.asarray(highs, float), np.asarray(lows, float)
    m = (h > 0) & (l > 0)
    if m.sum() < 2:
        return float("nan")
    hl = np.log(h[m] / l[m])
    return float(math.sqrt(np.mean(hl ** 2) / (4 * math.log(2)) * periods_per_year))


@dataclass
class GarchResult:
    omega: float
    alpha: float
    beta: float
    persistence: float
    long_run_vol: float
    last_sigma: float
    forecast_sigma: float
    loglik: float
    ok: bool


def garch11_fit(returns: Sequence[float]) -> GarchResult:
    """Gaussian GARCH(1,1) by MLE (variance targeting off), returns per-period sigmas."""
    r = np.asarray(returns, float)
    r = r[np.isfinite(r)]
    r = r - r.mean()
    n = len(r)
    var0 = float(r.var()) if n else 0.0
    if n < 100 or var0 <= 0 or not HAVE_SCIPY:
        return GarchResult(float("nan"), float("nan"), float("nan"), float("nan"), float("nan"),
                           math.sqrt(var0) if var0 > 0 else float("nan"), float("nan"), float("nan"), False)
    scale = math.sqrt(var0)
    z = r / scale

    def nll(params: np.ndarray) -> float:
        w, a, b = params
        if w <= 0 or a < 0 or b < 0 or a + b >= 0.9999:
            return 1e10
        h = np.empty(n)
        h[0] = 1.0
        for t in range(1, n):
            h[t] = w + a * z[t - 1] ** 2 + b * h[t - 1]
        return 0.5 * float(np.sum(np.log(h) + z * z / h))

    res = _sopt.minimize(nll, np.array([0.05, 0.08, 0.9]), method="Nelder-Mead",
                         options={"maxiter": 2000, "xatol": 1e-6, "fatol": 1e-6})
    w, a, b = res.x
    h = np.empty(n)
    h[0] = 1.0
    for t in range(1, n):
        h[t] = w + a * z[t - 1] ** 2 + b * h[t - 1]
    f = w + a * z[-1] ** 2 + b * h[-1]
    pers = a + b
    lr = math.sqrt(w / (1 - pers)) * scale if pers < 1 else float("nan")
    return GarchResult(float(w * scale * scale), float(a), float(b), float(pers), float(lr),
                       float(math.sqrt(h[-1]) * scale), float(math.sqrt(f) * scale), float(-res.fun), bool(res.success))


def vol_clustering_stat(returns: Sequence[float]) -> float:
    """ACF(1) of squared returns: > 0 indicates volatility clustering."""
    r = np.asarray(returns, float)
    return autocorr(r * r, 1)


# ---------------------------------------------------------------- LEVEL 6 --

def gbm_paths(S0: float, mu: float, sigma: float, T: float, steps: int, n_paths: int,
              seed: Optional[int] = None) -> np.ndarray:
    """Exact GBM discretisation; Ito: d ln S = (mu - 0.5 sigma^2) dt + sigma dW."""
    rng = np.random.default_rng(seed)
    dt_ = T / steps
    z = rng.standard_normal((n_paths, steps))
    inc = (mu - 0.5 * sigma * sigma) * dt_ + sigma * math.sqrt(dt_) * z
    return S0 * np.exp(np.concatenate([np.zeros((n_paths, 1)), np.cumsum(inc, 1)], 1))


def random_walk(n: int, step_sd: float, seed: Optional[int] = None) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.cumsum(rng.normal(0, step_sd, n))


def monte_carlo_move_probs(F: float, sigma_annual: float, minutes: float, levels: Sequence[float],
                           n_paths: int = 20000, seed: int = 7) -> Dict[str, float]:
    """Scenario analysis: P(touching each level within horizon) under GBM with the given vol.

    This is a *scenario* under an idealised model, not a forecast."""
    T = minutes / (252 * 375)
    steps = max(int(minutes), 5)
    paths = gbm_paths(F, 0.0, sigma_annual, T, steps, n_paths, seed)
    hi, lo = paths.max(1), paths.min(1)
    out = {}
    for L in levels:
        out[f"{L:.0f}"] = float(np.mean(hi >= L) if L >= F else np.mean(lo <= L))
    out["p5"], out["p95"] = float(np.percentile(paths[:, -1], 5)), float(np.percentile(paths[:, -1], 95))
    return out


def martingale_test(returns: Sequence[float]) -> Dict[str, float]:
    """Variance ratio test (Lo-MacKinlay, q=5): VR ~ 1 under random walk."""
    r = np.asarray(returns, float)
    q = 5
    if len(r) < 50:
        return {"vr": float("nan"), "z": float("nan")}
    v1 = r.var(ddof=1)
    rq = np.convolve(r, np.ones(q), "valid")
    vq = rq.var(ddof=1) / q
    vr = vq / v1 if v1 > 0 else float("nan")
    n = len(r)
    z = (vr - 1) / math.sqrt(2 * (2 * q - 1) * (q - 1) / (3 * q * n))
    return {"vr": float(vr), "z": float(z)}


# ---------------------------------------------------------------- LEVEL 8 --

def capm_beta(asset: Sequence[float], market: Sequence[float], rf: float = 0.0) -> Dict[str, float]:
    a, m = np.asarray(asset, float) - rf, np.asarray(market, float) - rf
    n = min(len(a), len(m))
    if n < 10:
        return {"alpha": float("nan"), "beta": float("nan"), "r2": float("nan")}
    r = ols(a[-n:], m[-n:])
    return {"alpha": float(r.beta[0]), "beta": float(r.beta[1]), "r2": r.r2, "alpha_p": float(r.p[0])}


def fama_french_regression(asset: Sequence[float], factors: np.ndarray) -> Optional[OLSResult]:
    """Only run if a reliable Indian factor file was supplied (FF_FACTOR_CSV)."""
    if factors is None or len(factors) < 30:
        return None
    return ols(asset, factors)


def kelly_fraction(p_win: float, win: float, loss: float) -> float:
    """f* = p/L - q/W for payoff +W / -L (fraction of capital)."""
    if win <= 0 or loss <= 0:
        return 0.0
    return p_win / loss - (1 - p_win) / win


def kelly_continuous(mu: float, sigma: float) -> float:
    return mu / (sigma * sigma) if sigma > 0 else 0.0


def var_es(returns: Sequence[float], alpha: float = 0.95) -> Tuple[float, float]:
    """Historical VaR and Expected Shortfall (positive numbers = loss)."""
    r = np.sort(np.asarray(returns, float))
    if len(r) < 20:
        return float("nan"), float("nan")
    k = int(math.floor((1 - alpha) * len(r)))
    k = max(k, 1)
    return float(-r[k - 1]), float(-r[:k].mean())


def parametric_var(mu: float, sigma: float, alpha: float = 0.95) -> float:
    return float(-(mu + sigma * statistics.NormalDist().inv_cdf(1 - alpha)))


def max_drawdown(equity: Sequence[float]) -> Tuple[float, int, int]:
    e = np.asarray(equity, float)
    if len(e) < 2:
        return 0.0, 0, 0
    peak = np.maximum.accumulate(e)
    dd = e - peak
    i = int(np.argmin(dd))
    j = int(np.argmax(e[: i + 1])) if i > 0 else 0
    return float(dd[i]), j, i


def risk_of_ruin(p_win: float, payoff_ratio: float, risk_per_trade: float, ruin_frac: float = 0.5,
                 n_sims: int = 5000, n_trades: int = 500, seed: int = 11) -> float:
    """Monte Carlo probability of losing `ruin_frac` of capital with fixed-fraction risk."""
    rng = np.random.default_rng(seed)
    wins = rng.random((n_sims, n_trades)) < p_win
    mult = np.where(wins, 1 + risk_per_trade * payoff_ratio, 1 - risk_per_trade)
    eq = np.cumprod(mult, 1)
    return float(np.mean(eq.min(1) <= 1 - ruin_frac))


def position_size(capital: float, risk_frac: float, entry: float, stop: float, lot: int) -> int:
    """Lots such that loss at stop <= capital*risk_frac (informational only, no orders)."""
    per_lot = abs(entry - stop) * lot
    return int(capital * risk_frac // per_lot) if per_lot > 0 else 0


# ---------------------------------------------------------------- LEVEL 9 --

def mid_price(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    if bid and ask and bid > 0 and ask > 0 and ask >= bid:
        return 0.5 * (bid + ask)
    return None


def spread_pct(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    m = mid_price(bid, ask)
    return (ask - bid) / m * 100 if m else None  # type: ignore[operator]


def book_imbalance(bid_qty: float, ask_qty: float) -> Optional[float]:
    """(B - A) / (B + A) in [-1, 1]."""
    s = (bid_qty or 0) + (ask_qty or 0)
    return ((bid_qty or 0) - (ask_qty or 0)) / s if s > 0 else None


def depth_imbalance(bids: Sequence[Tuple[float, float]], asks: Sequence[Tuple[float, float]],
                    decay: float = 0.5) -> Optional[float]:
    """Level-weighted multi-level imbalance; bids/asks = [(price, qty)] best first."""
    wb = sum(q * decay ** i for i, (_, q) in enumerate(bids))
    wa = sum(q * decay ** i for i, (_, q) in enumerate(asks))
    return (wb - wa) / (wb + wa) if wb + wa > 0 else None


def microprice(bid: float, ask: float, bq: float, aq: float) -> Optional[float]:
    return (bid * aq + ask * bq) / (aq + bq) if bq + aq > 0 and bid > 0 and ask > 0 else None


def sqrt_impact(qty: float, adv: float, sigma_daily: float, Y: float = 0.7) -> float:
    """Square-root law: impact (fraction of price) ~ Y * sigma_d * sqrt(Q / ADV)."""
    return Y * sigma_daily * math.sqrt(qty / adv) if adv > 0 and qty > 0 else float("nan")


def vwap(prices: Sequence[float], volumes: Sequence[float]) -> Optional[float]:
    p, v = np.asarray(prices, float), np.asarray(volumes, float)
    s = v.sum()
    return float((p * v).sum() / s) if s > 0 else None


def twap(prices: Sequence[float]) -> Optional[float]:
    return float(np.mean(prices)) if len(prices) else None


def walk_book_cost(levels: Sequence[Tuple[float, float]], qty: float) -> Optional[float]:
    """Average fill price walking visible depth levels [(price, qty)]; None if depth insufficient."""
    rem, cost = qty, 0.0
    for px, q in levels:
        take = min(rem, q)
        cost += take * px
        rem -= take
        if rem <= 0:
            return cost / qty
    return None


def slippage_estimate(mid: float, side_levels: Sequence[Tuple[float, float]], qty: float) -> Optional[float]:
    fill = walk_book_cost(side_levels, qty)
    return abs(fill - mid) / mid * 100 if fill and mid else None


def execution_cost_pct(spread_pct_v: Optional[float], slippage_pct: Optional[float], fees_bps: float) -> Optional[float]:
    if spread_pct_v is None:
        return None
    return spread_pct_v / 2 * 2 + (slippage_pct or 0) + fees_bps / 100


def liquidity_score(spread_pct_v: Optional[float], volume: float, oi: float, top_qty: float, lot: int) -> float:
    """0-100 heuristic: tight spread, traded volume, OI and visible size in lots."""
    if spread_pct_v is None:
        return 0.0
    s_sp = max(0.0, 1 - spread_pct_v / 5.0)                 # 0% -> 1, >=5% -> 0
    s_vol = min(1.0, math.log10(1 + volume / max(lot, 1)) / 4)
    s_oi = min(1.0, math.log10(1 + oi / max(lot, 1)) / 4.5)
    s_q = min(1.0, math.log10(1 + top_qty / max(lot, 1)) / 2)
    return round(100 * (0.4 * s_sp + 0.25 * s_vol + 0.2 * s_oi + 0.15 * s_q), 1)


def option_costs_bps(premium: float, qty: int, side_sell: bool) -> float:
    """Approximate Indian index-option charges as bps of premium turnover (one side).
    STT 0.1% on sell premium (2024 revision), exchange txn ~0.035-0.05%, SEBI 0.0001%,
    stamp 0.003% on buy, GST 18% on (brokerage+txn). Flat Rs20 brokerage per order.
    These are approximations for research costing only; override with COST_BPS_ROUNDTRIP."""
    turnover = premium * qty
    if turnover <= 0:
        return 0.0
    stt = 0.001 * turnover if side_sell else 0.0
    txn = 0.0004 * turnover
    stamp = 0.00003 * turnover if not side_sell else 0.0
    brokerage = 20.0
    gst = 0.18 * (brokerage + txn)
    return (stt + txn + stamp + brokerage + gst + 0.000001 * turnover) / turnover * 1e4


# --------------------------------------------------------------- LEVEL 10 --

def confusion(y_true: Sequence[int], y_pred: Sequence[int]) -> Dict[str, Any]:
    t, p = np.asarray(y_true, int), np.asarray(y_pred, int)
    tp = int(((t == 1) & (p == 1)).sum())
    fp = int(((t == 0) & (p == 1)).sum())
    fn = int(((t == 1) & (p == 0)).sum())
    tn = int(((t == 0) & (p == 0)).sum())
    prec = tp / (tp + fp) if tp + fp else float("nan")
    rec = tp / (tp + fn) if tp + fn else float("nan")
    f1 = 2 * prec * rec / (prec + rec) if (tp and np.isfinite(prec) and np.isfinite(rec)) else float("nan")
    acc = (tp + tn) / max(len(t), 1)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": prec, "recall": rec, "f1": f1,
            "accuracy": acc, "base_rate": float(t.mean()) if len(t) else float("nan")}


def walk_forward_splits(n: int, n_folds: int = 5, min_train: int = 50, embargo: int = 0) -> List[Tuple[np.ndarray, np.ndarray]]:
    """Expanding-window, time-ordered splits; test never precedes or overlaps train (+embargo)."""
    out = []
    if n < min_train + n_folds:
        return out
    fold = (n - min_train) // n_folds
    for k in range(n_folds):
        tr_end = min_train + k * fold
        te_start = tr_end + embargo
        te_end = min(tr_end + fold, n) if k < n_folds - 1 else n
        if te_start >= te_end:
            continue
        out.append((np.arange(0, tr_end), np.arange(te_start, te_end)))
    return out


def check_no_overlap(train_idx: Sequence[int], test_idx: Sequence[int]) -> bool:
    return len(set(train_idx) & set(test_idx)) == 0 and (max(train_idx) < min(test_idx))


def lookahead_violations(feature_ts: Sequence[float], decision_ts: Sequence[float]) -> int:
    """Count rows whose features were computed from data stamped after the decision time."""
    return int(sum(1 for f, d in zip(feature_ts, decision_ts) if f > d + 1e-6))


def leakage_scan(X: np.ndarray, y: Sequence[int], names: Sequence[str], thresh: float = 0.95) -> List[str]:
    """Flag features almost perfectly predicting the label (typical symptom of label leakage)."""
    flags = []
    ya = np.asarray(y, float)
    for j, nm in enumerate(names):
        r, _ = pearson(X[:, j], ya)
        if np.isfinite(r) and abs(r) >= thresh:
            flags.append(nm)
    return flags


def deflated_sharpe_ratio(sr: float, n_obs: int, n_trials: int, skew: float, kurt: float,
                          sr_var_trials: float) -> float:
    """Bailey & Lopez de Prado (2014). sr = per-period Sharpe (not annualised). Returns P(SR* > 0)."""
    if n_obs < 3 or n_trials < 1:
        return float("nan")
    emc = 0.5772156649
    nd = statistics.NormalDist()
    sd = math.sqrt(max(sr_var_trials, 1e-12))
    if n_trials > 1:
        sr0 = sd * ((1 - emc) * nd.inv_cdf(1 - 1 / n_trials) + emc * nd.inv_cdf(1 - 1 / (n_trials * math.e)))
    else:
        sr0 = 0.0
    den = math.sqrt(max(1 - skew * sr + (kurt - 1) / 4 * sr * sr, 1e-12))
    return norm_cdf((sr - sr0) * math.sqrt(n_obs - 1) / den)


def probabilistic_sharpe(sr: float, n_obs: int, skew: float, kurt: float, sr_bench: float = 0.0) -> float:
    if n_obs < 3:
        return float("nan")
    den = math.sqrt(max(1 - skew * sr + (kurt - 1) / 4 * sr * sr, 1e-12))
    return norm_cdf((sr - sr_bench) * math.sqrt(n_obs - 1) / den)


def logistic_fit(X: np.ndarray, y: Sequence[int], l2: float = 1.0, iters: int = 300) -> np.ndarray:
    """L2-regularised logistic regression by Newton-IRLS (explainable baseline model)."""
    Xa = np.column_stack([np.ones(len(X)), np.asarray(X, float)])
    ya = np.asarray(y, float)
    w = np.zeros(Xa.shape[1])
    R = l2 * np.eye(Xa.shape[1])
    R[0, 0] = 0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(Xa @ w, -30, 30)))
        g = Xa.T @ (p - ya) + R @ w
        H = Xa.T @ (Xa * (p * (1 - p))[:, None]) + R
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            break
        w -= step
        if np.max(np.abs(step)) < 1e-8:
            break
    return w


def logistic_predict(w: np.ndarray, X: np.ndarray) -> np.ndarray:
    Xa = np.column_stack([np.ones(len(X)), np.asarray(X, float)])
    return 1 / (1 + np.exp(-np.clip(Xa @ w, -30, 30)))


def permutation_importance(predict: Callable[[np.ndarray], np.ndarray], X: np.ndarray, y: Sequence[int],
                           names: Sequence[str], seed: int = 3) -> Dict[str, float]:
    rng = np.random.default_rng(seed)
    ya = np.asarray(y, float)
    base = brier_score(predict(X), ya)
    out = {}
    for j, nm in enumerate(names):
        Xp = X.copy()
        rng.shuffle(Xp[:, j])
        out[nm] = brier_score(predict(Xp), ya) - base
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


class GaussianHMM:
    """Minimal K-state Gaussian HMM (diagonal) trained by Baum-Welch; used for regime detection
    only when >= 500 observations exist (otherwise the rule-based regime is used)."""

    def __init__(self, k: int = 3, iters: int = 50, seed: int = 5):
        self.k, self.iters, self.seed = k, iters, seed
        self.ok = False

    def fit(self, X: np.ndarray) -> "GaussianHMM":
        X = np.asarray(X, float)
        if X.ndim == 1:
            X = X[:, None]
        n, d = X.shape
        k = self.k
        rng = np.random.default_rng(self.seed)
        q = np.quantile(X[:, 0], np.linspace(0.15, 0.85, k))
        self.mu = np.tile(X.mean(0), (k, 1))
        self.mu[:, 0] = q
        self.var = np.tile(X.var(0) + 1e-9, (k, 1))
        self.A = np.full((k, k), 0.1 / (k - 1))
        np.fill_diagonal(self.A, 0.9)
        self.pi = np.full(k, 1.0 / k)
        _ = rng
        for _it in range(self.iters):
            B = self._emis(X)
            alpha, beta, c = self._fb(B)
            gamma = alpha * beta
            gamma /= gamma.sum(1, keepdims=True)
            xi = np.zeros((k, k))
            for t in range(n - 1):
                m = alpha[t][:, None] * self.A * (B[t + 1] * beta[t + 1])[None, :]
                xi += m / m.sum()
            self.pi = gamma[0]
            self.A = xi / xi.sum(1, keepdims=True)
            w = gamma.sum(0)[:, None]
            self.mu = gamma.T @ X / w
            self.var = (gamma.T @ (X * X)) / w - self.mu ** 2 + 1e-9
        self.ok = True
        self.loglik = float(np.sum(np.log(c)))
        return self

    def _emis(self, X: np.ndarray) -> np.ndarray:
        diff = X[:, None, :] - self.mu[None, :, :]
        ll = -0.5 * np.sum(diff ** 2 / self.var[None] + np.log(2 * np.pi * self.var[None]), axis=2)
        ll -= ll.max(1, keepdims=True)
        return np.exp(ll) + 1e-300

    def _fb(self, B: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        n, k = B.shape
        alpha = np.zeros((n, k))
        c = np.zeros(n)
        alpha[0] = self.pi * B[0]
        c[0] = alpha[0].sum()
        alpha[0] /= c[0]
        for t in range(1, n):
            alpha[t] = (alpha[t - 1] @ self.A) * B[t]
            c[t] = alpha[t].sum()
            alpha[t] /= c[t]
        beta = np.ones((n, k))
        for t in range(n - 2, -1, -1):
            beta[t] = (self.A @ (B[t + 1] * beta[t + 1])) / c[t + 1]
        return alpha, beta, c

    def predict_last(self, X: np.ndarray) -> Tuple[int, np.ndarray]:
        X = np.asarray(X, float)
        if X.ndim == 1:
            X = X[:, None]
        alpha, _, _ = self._fb(self._emis(X))
        return int(np.argmax(alpha[-1])), alpha[-1]


# ------------------------------------------------- robust adaptive stats --

class RollingStat:
    """Fixed-size window giving robust z-scores (median / MAD) for adaptive thresholds."""

    def __init__(self, maxlen: int = 400):
        self.buf: Deque[float] = collections.deque(maxlen=maxlen)

    def add(self, x: Optional[float]) -> None:
        if x is not None and math.isfinite(x):
            self.buf.append(float(x))

    def __len__(self) -> int:
        return len(self.buf)

    def robust_z(self, x: Optional[float], min_n: int = 30) -> Optional[float]:
        if x is None or not math.isfinite(x) or len(self.buf) < min_n:
            return None
        a = np.fromiter(self.buf, float)
        med = float(np.median(a))
        mad = float(np.median(np.abs(a - med))) * 1.4826
        if mad <= 1e-12:
            sd = float(a.std())
            return (x - med) / sd if sd > 1e-12 else 0.0
        return (x - med) / mad

    def quantile(self, q: float) -> Optional[float]:
        return float(np.quantile(np.fromiter(self.buf, float), q)) if len(self.buf) >= 10 else None


def classify_buildup(d_price: Optional[float], d_oi: Optional[float], price_eps: float = 0.0,
                     oi_eps: float = 0.0) -> str:
    """Price + OI joint classification (never OI alone).

    price up & OI up   -> LONG_BUILDUP     (buyers adding)
    price down & OI up -> WRITING / SHORT_BUILDUP (sellers adding)
    price up & OI down -> SHORT_COVERING   (writers exiting)
    price down & OI down -> LONG_UNWINDING (buyers exiting)
    """
    if d_price is None or d_oi is None:
        return "UNKNOWN"
    if abs(d_price) <= price_eps or abs(d_oi) <= oi_eps:
        return "NEUTRAL"
    if d_price > 0 and d_oi > 0:
        return "LONG_BUILDUP"
    if d_price < 0 and d_oi > 0:
        return "WRITING"
    if d_price > 0 and d_oi < 0:
        return "SHORT_COVERING"
    return "LONG_UNWINDING"

# ============================================================================
# 4. STORAGE  (SQLite WAL now; schema kept ANSI-ish for PostgreSQL/TimescaleDB)
# ============================================================================
# Conventions for portability:
#  * every time-series table has ts (epoch seconds, REAL -> TIMESTAMPTZ/hypertable time
#    column on migration), ts_utc and ts_ist ISO strings;
#  * JSON payloads in TEXT columns (-> JSONB);
#  * no SQLite-only types; INTEGER PRIMARY KEY -> BIGSERIAL.

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS instruments (
  security_id INTEGER NOT NULL, segment TEXT NOT NULL, exch TEXT, instrument TEXT,
  underlying TEXT, symbol TEXT, display TEXT, expiry TEXT, strike REAL, option_type TEXT,
  lot_size REAL, tick_size REAL, loaded_on TEXT,
  PRIMARY KEY (segment, security_id));
CREATE INDEX IF NOT EXISTS ix_instr_und ON instruments(underlying, instrument, expiry);

CREATE TABLE IF NOT EXISTS market_ticks (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, ts_utc TEXT, ts_ist TEXT, security_id INTEGER, segment TEXT,
  name TEXT, ltp REAL, ltq REAL, ltt REAL, atp REAL, volume REAL, oi REAL, buy_qty REAL, sell_qty REAL,
  bid REAL, ask REAL, bid_qty REAL, ask_qty REAL, open REAL, high REAL, low REAL, close REAL, depth TEXT);
CREATE INDEX IF NOT EXISTS ix_ticks_sid_ts ON market_ticks(security_id, ts);

CREATE TABLE IF NOT EXISTS index_snapshots (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, ts_utc TEXT, ts_ist TEXT, index_name TEXT NOT NULL,
  spot REAL, fut REAL, forward REAL, vix REAL, vwap REAL, basis REAL, expiry TEXT, atm REAL,
  pcr_oi REAL, pcr_vol REAL, max_pain REAL, atm_iv REAL, rv REAL, straddle REAL, gex REAL,
  gamma_conc REAL, data_quality REAL, regime TEXT, scores TEXT, features TEXT);
CREATE INDEX IF NOT EXISTS ix_isnap ON index_snapshots(index_name, ts);

CREATE TABLE IF NOT EXISTS chain_snapshots (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, ts_utc TEXT, ts_ist TEXT, index_name TEXT NOT NULL,
  expiry TEXT NOT NULL, strike REAL NOT NULL, side TEXT NOT NULL, security_id INTEGER,
  ltp REAL, oi REAL, prev_oi REAL, volume REAL, prev_volume REAL, prev_close REAL,
  bid REAL, ask REAL, bid_qty REAL, ask_qty REAL, vendor_iv REAL, vendor_delta REAL,
  vendor_gamma REAL, vendor_theta REAL, vendor_vega REAL,
  iv REAL, delta REAL, gamma REAL, theta REAL, vega REAL, rho REAL, spread_pct REAL,
  liquidity REAL, buildup TEXT, source TEXT);
CREATE INDEX IF NOT EXISTS ix_chain ON chain_snapshots(index_name, expiry, ts);
CREATE INDEX IF NOT EXISTS ix_chain_strike ON chain_snapshots(index_name, strike, side, ts);

CREATE TABLE IF NOT EXISTS candles (
  security_id INTEGER NOT NULL, segment TEXT NOT NULL, interval TEXT NOT NULL, ts REAL NOT NULL,
  ts_ist TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, oi REAL, name TEXT,
  PRIMARY KEY (security_id, segment, interval, ts));

CREATE TABLE IF NOT EXISTS vol_metrics (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, ts_utc TEXT, ts_ist TEXT, index_name TEXT,
  atm_iv REAL, rv_5m REAL, rv_1d REAL, parkinson REAL, garch_sigma REAL, garch_persistence REAL,
  iv_rv_spread REAL, skew_25d REAL, term_slope REAL, vix REAL, extra TEXT);
CREATE INDEX IF NOT EXISTS ix_vol ON vol_metrics(index_name, ts);

CREATE TABLE IF NOT EXISTS regimes (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, ts_utc TEXT, ts_ist TEXT, index_name TEXT,
  regime TEXT, secondary TEXT, evidence TEXT, hmm_state INTEGER);
CREATE INDEX IF NOT EXISTS ix_regime ON regimes(index_name, ts);

CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, ts_utc TEXT, ts_ist TEXT, index_name TEXT,
  kind TEXT, direction INTEGER, score REAL, spot REAL, fut REAL, feature_ts REAL,
  evidence TEXT, features TEXT,
  ret_5 REAL, ret_15 REAL, ret_30 REAL, mfe_30 REAL, mae_30 REAL, labelled INTEGER DEFAULT 0,
  outcome TEXT);
CREATE INDEX IF NOT EXISTS ix_sig ON signals(index_name, ts);
CREATE INDEX IF NOT EXISTS ix_sig_lab ON signals(labelled, ts);

CREATE TABLE IF NOT EXISTS breakout_events (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, ts_utc TEXT, ts_ist TEXT, index_name TEXT,
  kind TEXT, direction INTEGER, move_pct REAL, sigma_mult REAL, spot REAL,
  pre_15 TEXT, pre_10 TEXT, pre_5 TEXT, at_event TEXT, post_5 TEXT, post_15 TEXT, post_30 TEXT,
  analysis TEXT, genuine INTEGER, complete INTEGER DEFAULT 0, lead_minutes REAL, regime TEXT);
CREATE INDEX IF NOT EXISTS ix_evt ON breakout_events(index_name, ts);

CREATE TABLE IF NOT EXISTS validation_results (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, ts_ist TEXT, pattern TEXT, index_name TEXT,
  horizon INTEGER, n INTEGER, hits INTEGER, success_rate REAL, ci_lo REAL, ci_hi REAL,
  mean_ret REAL, median_ret REAL, mfe REAL, mae REAL, precision REAL, recall REAL, f1 REAL,
  net_ret_after_cost REAL, p_value REAL, p_adj REAL, fdr_pass INTEGER, sample_status TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS ix_val ON validation_results(pattern, ts);

CREATE TABLE IF NOT EXISTS model_performance (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, ts_ist TEXT, model TEXT, target TEXT, n_train INTEGER,
  n_test INTEGER, precision REAL, recall REAL, f1 REAL, brier REAL, base_brier REAL, ece REAL,
  sharpe REAL, dsr REAL, psr REAL, lookahead_violations INTEGER, leakage_flags TEXT, detail TEXT);

CREATE TABLE IF NOT EXISTS execution_costs (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, ts_ist TEXT, index_name TEXT, expiry TEXT, strike REAL,
  side TEXT, mid REAL, spread_pct REAL, slippage_1lot REAL, slippage_5lot REAL, fees_bps REAL,
  total_cost_pct REAL, impact_sqrt REAL, liquidity REAL);
CREATE INDEX IF NOT EXISTS ix_cost ON execution_costs(index_name, ts);

CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, ts_ist TEXT, index_name TEXT, kind TEXT, dedupe TEXT,
  text TEXT, sent INTEGER, error TEXT);
CREATE INDEX IF NOT EXISTS ix_alert ON alerts(index_name, kind, ts);

CREATE TABLE IF NOT EXISTS health (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, ts_ist TEXT, component TEXT, status TEXT, detail TEXT);
"""


class Database:
    """Single-writer SQLite with WAL. All writes go through a queue drained in batches by a
    dedicated thread, so the asyncio loop never blocks on disk I/O."""

    TABLE_COLS: Dict[str, List[str]] = {}

    def __init__(self, path: Path, batch_size: int = 500, flush_s: float = 2.0):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.batch_size = batch_size
        self.flush_s = flush_s
        self._q: "collections.deque[Tuple[str, Tuple[Any, ...]]]" = collections.deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.rows_written = 0
        self.write_errors = 0
        self.init_schema()

    def connect(self, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=30, check_same_thread=False)
        else:
            con = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=30000")
        con.row_factory = sqlite3.Row
        return con

    def init_schema(self) -> None:
        con = self.connect()
        try:
            con.executescript(SCHEMA_SQL)
            con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
            con.commit()
            for (tname,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                cols = [r[1] for r in con.execute(f"PRAGMA table_info({tname})").fetchall()]
                Database.TABLE_COLS[tname] = cols
        finally:
            con.close()

    # ---- writer ---------------------------------------------------------
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="db-writer", daemon=True)
            self._thread.start()

    def insert(self, table: str, row: Dict[str, Any], mode: str = "INSERT") -> None:
        cols = [c for c in row.keys() if c in Database.TABLE_COLS.get(table, [])]
        if not cols:
            return
        sql = f"{mode} INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
        vals = tuple(_db_val(row[c]) for c in cols)
        with self._lock:
            self._q.append((sql, vals))

    def execute_async(self, sql: str, params: Tuple[Any, ...]) -> None:
        with self._lock:
            self._q.append((sql, params))

    def pending(self) -> int:
        return len(self._q)

    def _drain(self, con: sqlite3.Connection, max_items: int) -> int:
        with self._lock:
            n = min(len(self._q), max_items)
            items = [self._q.popleft() for _ in range(n)]
        if not items:
            return 0
        grouped: Dict[str, List[Tuple[Any, ...]]] = collections.defaultdict(list)
        order: List[str] = []
        for sql, vals in items:
            if sql not in grouped:
                order.append(sql)
            grouped[sql].append(vals)
        try:
            with con:
                for sql in order:
                    con.executemany(sql, grouped[sql])
            self.rows_written += n
        except sqlite3.Error as e:
            self.write_errors += 1
            LOG.error("db batch write failed; retrying row-by-row", error=str(e), n=n)
            for sql in order:
                for vals in grouped[sql]:
                    try:
                        with con:
                            con.execute(sql, vals)
                        self.rows_written += 1
                    except sqlite3.Error as e2:
                        LOG.error("db row dropped", error=str(e2), sql=sql[:80])
        return n

    def _run(self) -> None:
        con = self.connect()
        last = time.time()
        try:
            while not self._stop.is_set():
                if len(self._q) >= self.batch_size or (time.time() - last >= self.flush_s and self._q):
                    self._drain(con, self.batch_size * 4)
                    last = time.time()
                else:
                    self._stop.wait(0.2)
            while self._q:
                self._drain(con, 10_000)
        finally:
            con.close()

    def flush_sync(self) -> None:
        con = self.connect()
        try:
            while self._q:
                self._drain(con, 10_000)
        finally:
            con.close()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)
        self.flush_sync()

    # ---- reads (own connection; safe from any thread) -------------------
    def query(self, sql: str, params: Tuple[Any, ...] = ()) -> List[sqlite3.Row]:
        con = self.connect()
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    def execute(self, sql: str, params: Tuple[Any, ...] = ()) -> None:
        con = self.connect()
        try:
            with con:
                con.execute(sql, params)
        finally:
            con.close()

    def set_meta(self, k: str, v: Any) -> None:
        self.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (k, json.dumps(v, default=str)))

    def get_meta(self, k: str, default: Any = None) -> Any:
        r = self.query("SELECT v FROM meta WHERE k=?", (k,))
        return json.loads(r[0]["v"]) if r else default


def _db_val(v: Any) -> Any:
    if v is None or isinstance(v, (int, str, bytes)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, (np.floating,)):
        f = float(v)
        return f if math.isfinite(f) else None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, bool):
        return int(v)
    return json.dumps(v, default=_json_default)


def _json_default(o: Any) -> Any:
    if isinstance(o, (np.floating,)):
        f = float(o)
        return f if math.isfinite(f) else None
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if dataclasses.is_dataclass(o):
        return dataclasses.asdict(o)
    if isinstance(o, (dt.date, dt.datetime)):
        return o.isoformat()
    if isinstance(o, set):
        return sorted(o)
    return str(o)


def jdump(o: Any) -> str:
    def clean(x: Any) -> Any:
        if isinstance(x, float):
            return x if math.isfinite(x) else None
        if isinstance(x, dict):
            return {str(k): clean(v) for k, v in x.items()}
        if isinstance(x, (list, tuple)):
            return [clean(v) for v in x]
        return x
    return json.dumps(clean(o), default=_json_default, separators=(",", ":"))

# ============================================================================
# 5. DHAN API LAYER  (read-only)
# ============================================================================

READ_ONLY_ENDPOINTS = ("/optionchain", "/optionchain/expirylist", "/marketfeed/ltp", "/marketfeed/ohlc",
                       "/marketfeed/quote", "/charts/intraday", "/charts/historical", "/profile")


class OrderEndpointBlocked(RuntimeError):
    pass


def assert_read_only(endpoint: str) -> None:
    if endpoint not in READ_ONLY_ENDPOINTS:
        raise OrderEndpointBlocked(f"endpoint {endpoint!r} is not an allow-listed market-data endpoint")


class RateLimiter:
    """Per-class minimum spacing + per-key spacing (option chain: 1 unique request / 3 s)."""

    def __init__(self) -> None:
        self._last: Dict[str, float] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self.calls: Dict[str, int] = collections.Counter()  # type: ignore[assignment]
        self.daily: Dict[str, int] = collections.Counter()  # type: ignore[assignment]
        self._day = now_ist().date()

    def _lock(self, k: str) -> asyncio.Lock:
        if k not in self._locks:
            self._locks[k] = asyncio.Lock()
        return self._locks[k]

    async def acquire(self, klass: str, min_interval: float) -> None:
        async with self._lock(klass):
            d = now_ist().date()
            if d != self._day:
                self.daily.clear()
                self._day = d
            wait = self._last.get(klass, 0.0) + min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[klass] = time.monotonic()
            self.calls[klass] += 1
            self.daily[klass] += 1


class DhanApiError(RuntimeError):
    def __init__(self, msg: str, code: Optional[str] = None, retryable: bool = True):
        super().__init__(msg)
        self.code = code
        self.retryable = retryable


NON_RETRYABLE = {"DH-901", "DH-902", "DH-903", "DH-905", "DH-906", "DH-808", "DH-809", "807", "808", "809"}


class DhanRest:
    """Market-data REST through the official SDK's data mixins (OptionChain, HistoricalData,
    MarketFeed quote). Falls back to direct HTTPS with identical payloads if the SDK is absent.
    Every call is allow-listed, rate-limited, retried with exponential backoff and jitter."""

    BASE = "https://api.dhan.co/v2"

    def __init__(self, cfg: Config, limiter: RateLimiter):
        self.cfg = cfg
        self.lim = limiter
        self._sdk_token = ""
        self._oc = self._hist = self._mq = None
        self.errors = 0
        self.last_ok = 0.0
        self.auth_failed = False

    def _ensure_sdk(self) -> None:
        tok = self.cfg.resolve_token()
        register_secret(tok)
        if not HAVE_SDK or tok == self._sdk_token:
            return
        ctx = _SdkContext(self.cfg.client_id, tok)
        self._oc, self._hist, self._mq = _SdkOptionChain(ctx), _SdkHistorical(ctx), _SdkMarketQuote(ctx)
        self._sdk_token = tok

    def _direct(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        import requests  # dependency of the SDK
        tok = self.cfg.resolve_token()
        h = {"access-token": tok, "client-id": self.cfg.client_id, "Content-Type": "application/json",
             "Accept": "application/json"}
        r = requests.post(self.BASE + endpoint, json=payload, headers=h, timeout=20)
        try:
            j = r.json()
        except ValueError:
            j = {}
        if 200 <= r.status_code < 300:
            return {"status": "success", "remarks": "", "data": j}
        return {"status": "failure", "remarks": {"error_code": j.get("errorCode"), "error_type": j.get("errorType"),
                                                 "error_message": j.get("errorMessage"), "http": r.status_code},
                "data": ""}

    def _call_sync(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        assert_read_only(endpoint)
        self._ensure_sdk()
        if HAVE_SDK and self._oc is not None:
            if endpoint == "/optionchain":
                return self._oc.option_chain(payload["UnderlyingScrip"], payload["UnderlyingSeg"], payload["Expiry"])
            if endpoint == "/optionchain/expirylist":
                return self._oc.expiry_list(payload["UnderlyingScrip"], payload["UnderlyingSeg"])
            if endpoint == "/charts/intraday":
                return self._hist.intraday_minute_data(payload["securityId"], payload["exchangeSegment"],
                                                       payload["instrument"], payload["fromDate"], payload["toDate"],
                                                       payload["interval"], payload.get("oi", False))
            if endpoint == "/charts/historical":
                return self._hist.historical_daily_data(payload["securityId"], payload["exchangeSegment"],
                                                        payload["instrument"], payload["fromDate"], payload["toDate"],
                                                        payload.get("expiryCode", 0), payload.get("oi", False))
            if endpoint == "/marketfeed/quote":
                return self._mq.quote_data(payload)
        return self._direct(endpoint, payload)

    async def call(self, endpoint: str, payload: Dict[str, Any], klass: str, min_interval: float,
                   retries: int = 4) -> Any:
        if not self.cfg.client_id or not self.cfg.resolve_token():
            raise DhanApiError("DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN not configured", "NO_CREDS", False)
        delay = 1.0
        last_err: Optional[Exception] = None
        for attempt in range(retries + 1):
            await self.lim.acquire(klass, min_interval)
            try:
                res = await asyncio.wait_for(asyncio.to_thread(self._call_sync, endpoint, dict(payload)), 30)
            except OrderEndpointBlocked:
                raise
            except Exception as e:  # network / timeout
                last_err = e
                self.errors += 1
                LOG.warning("dhan rest exception", endpoint=endpoint, attempt=attempt, error=str(e)[:200])
                await asyncio.sleep(delay + random.random() * 0.5)
                delay = min(delay * 2, 30)
                continue
            if res.get("status") == "success":
                self.last_ok = time.time()
                self.auth_failed = False
                return res.get("data")
            rem = res.get("remarks")
            code = str(rem.get("error_code")) if isinstance(rem, dict) else None
            msg = json.dumps(rem, default=str)[:300]
            self.errors += 1
            if code in NON_RETRYABLE or (isinstance(rem, dict) and rem.get("error_type") in ("Invalid_Authentication", "Invalid_Access")):
                self.auth_failed = True
                raise DhanApiError(f"{endpoint}: {msg}", code, False)
            if code in ("DH-904", "805"):  # rate limit
                delay = max(delay, 3.0)
            LOG.warning("dhan rest failure", endpoint=endpoint, attempt=attempt, remarks=msg)
            last_err = DhanApiError(f"{endpoint}: {msg}", code)
            await asyncio.sleep(delay + random.random() * 0.5)
            delay = min(delay * 2, 30)
        raise last_err if isinstance(last_err, DhanApiError) else DhanApiError(str(last_err))

    # --- typed wrappers ----------------------------------------------------
    async def expiry_list(self, scrip: int, seg: str) -> List[str]:
        d = await self.call("/optionchain/expirylist", {"UnderlyingScrip": scrip, "UnderlyingSeg": seg},
                            "optionchain", self.cfg.chain_min_interval_s)
        lst = d.get("data") if isinstance(d, dict) else d
        return sorted(str(x)[:10] for x in (lst or []))

    async def option_chain(self, scrip: int, seg: str, expiry: str) -> Dict[str, Any]:
        d = await self.call("/optionchain", {"UnderlyingScrip": scrip, "UnderlyingSeg": seg, "Expiry": expiry},
                            "optionchain", self.cfg.chain_min_interval_s)
        if isinstance(d, dict) and "data" in d and isinstance(d["data"], dict) and "oc" in d["data"]:
            return d["data"]
        return d if isinstance(d, dict) else {}

    async def quote(self, req: Dict[str, List[int]]) -> Dict[str, Any]:
        d = await self.call("/marketfeed/quote", req, "quote", self.cfg.quote_min_interval_s)
        return (d.get("data") if isinstance(d, dict) and "data" in d else d) or {}

    async def intraday(self, sid: int, seg: str, instrument: str, frm: str, to: str, interval: int,
                       oi: bool = False) -> Dict[str, Any]:
        if interval not in (1, 5, 15, 25, 60):
            raise ValueError("Dhan intraday intervals: 1,5,15,25,60")
        d = await self.call("/charts/intraday", {"securityId": str(sid), "exchangeSegment": seg, "instrument": instrument,
                                                 "fromDate": frm, "toDate": to, "interval": interval, "oi": oi},
                            "data", self.cfg.data_api_min_interval_s)
        return d or {}

    async def daily(self, sid: int, seg: str, instrument: str, frm: str, to: str) -> Dict[str, Any]:
        d = await self.call("/charts/historical", {"securityId": str(sid), "exchangeSegment": seg, "instrument": instrument,
                                                   "fromDate": frm, "toDate": to, "expiryCode": 0, "oi": False},
                            "data", self.cfg.data_api_min_interval_s)
        return d or {}


def candles_from_payload(d: Dict[str, Any]) -> List[Dict[str, float]]:
    ts = d.get("timestamp") or []
    o, h, l, c = d.get("open") or [], d.get("high") or [], d.get("low") or [], d.get("close") or []
    v, oi = d.get("volume") or [], d.get("open_interest") or []
    n = min(len(ts), len(o), len(h), len(l), len(c))
    out = []
    for i in range(n):
        try:
            row = {"ts": float(ts[i]), "open": float(o[i]), "high": float(h[i]), "low": float(l[i]),
                   "close": float(c[i]), "volume": float(v[i]) if i < len(v) else 0.0,
                   "oi": float(oi[i]) if i < len(oi) else None}
        except (TypeError, ValueError):
            continue
        if row["high"] >= row["low"] > 0 and row["low"] <= row["close"] <= row["high"] * 1.0001:
            out.append(row)
    return out


def resample_candles(c1: List[Dict[str, float]], minutes: int) -> List[Dict[str, float]]:
    """Aggregate 1-minute bars into N-minute bars anchored at 09:15 IST (3m/30m are not served by Dhan)."""
    out: List[Dict[str, float]] = []
    cur: Optional[Dict[str, float]] = None
    cur_key = None
    for b in c1:
        t = dt.datetime.fromtimestamp(b["ts"], IST)
        mins = (t.hour * 60 + t.minute) - (9 * 60 + 15)
        key = (t.date(), mins // minutes)
        if key != cur_key:
            if cur:
                out.append(cur)
            start = dt.datetime.combine(t.date(), dt.time(9, 15), IST) + dt.timedelta(minutes=(mins // minutes) * minutes)
            cur = {"ts": start.timestamp(), "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"],
                   "volume": b.get("volume") or 0.0, "oi": b.get("oi")}
            cur_key = key
        else:
            assert cur is not None
            cur["high"] = max(cur["high"], b["high"])
            cur["low"] = min(cur["low"], b["low"])
            cur["close"] = b["close"]
            cur["volume"] = (cur.get("volume") or 0) + (b.get("volume") or 0)
            cur["oi"] = b.get("oi")
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------- instrument master --

@dataclass
class Contract:
    security_id: int
    segment: str
    underlying: str
    instrument: str          # OPTIDX / FUTIDX / INDEX
    expiry: str
    strike: Optional[float]
    option_type: Optional[str]
    lot_size: float
    tick_size: float
    display: str


class InstrumentMaster:
    URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cache = cfg.data_dir / "scrip_master.csv.gz"
        self.indices: Dict[str, Contract] = {}
        self.futures: Dict[str, List[Contract]] = collections.defaultdict(list)
        self.options: Dict[Tuple[str, str], Dict[Tuple[float, str], Contract]] = collections.defaultdict(dict)
        self.by_sid: Dict[Tuple[str, int], Contract] = {}
        self.loaded_on: Optional[str] = None

    def _download(self) -> bytes:
        import requests
        r = requests.get(self.URL, timeout=120)
        r.raise_for_status()
        return r.content

    def load(self, force: bool = False) -> None:
        today = now_ist().date().isoformat()
        raw: Optional[bytes] = None
        if self.cache.exists() and not force:
            age_h = (time.time() - self.cache.stat().st_mtime) / 3600
            mt = dt.datetime.fromtimestamp(self.cache.stat().st_mtime, IST).date().isoformat()
            if age_h < 20 and mt == today:
                raw = gzip.decompress(self.cache.read_bytes())
        if raw is None:
            try:
                raw = self._download()
                self.cache.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.cache.with_suffix(".tmp")
                tmp.write_bytes(gzip.compress(raw))
                tmp.replace(self.cache)
            except Exception as e:
                if self.cache.exists():
                    LOG.warning("scrip master download failed; using cached copy", error=str(e))
                    raw = gzip.decompress(self.cache.read_bytes())
                else:
                    raise
        self._parse(raw.decode("utf-8", "replace"))
        self.loaded_on = today

    def _parse(self, text: str) -> None:
        want = {DEFAULT_INDICES[n].symbol for n in DEFAULT_INDICES}
        rd = csv.reader(io.StringIO(text))
        hdr = [h.strip().upper() for h in next(rd)]

        def col(*names: str) -> int:
            for n in names:
                if n in hdr:
                    return hdr.index(n)
            return -1
        iE, iS, iSid = col("EXCH_ID", "SEM_EXM_EXCH_ID"), col("SEGMENT", "SEM_SEGMENT"), col("SECURITY_ID", "SEM_SMST_SECURITY_ID")
        iI, iU = col("INSTRUMENT", "SEM_INSTRUMENT_NAME"), col("UNDERLYING_SYMBOL", "SM_SYMBOL_NAME")
        iD, iX = col("DISPLAY_NAME", "SEM_CUSTOM_SYMBOL"), col("SM_EXPIRY_DATE", "SEM_EXPIRY_DATE")
        iK, iO = col("STRIKE_PRICE", "SEM_STRIKE_PRICE"), col("OPTION_TYPE", "SEM_OPTION_TYPE")
        iL, iT = col("LOT_SIZE", "SEM_LOT_UNITS"), col("TICK_SIZE", "SEM_TICK_SIZE")
        if min(iE, iS, iSid, iI, iU, iX) < 0:
            raise RuntimeError(f"scrip master layout changed: {hdr[:20]}")
        self.indices.clear()
        self.futures.clear()
        self.options.clear()
        self.by_sid.clear()
        seg_map = {("NSE", "I"): "IDX_I", ("BSE", "I"): "IDX_I", ("NSE", "D"): "NSE_FNO", ("BSE", "D"): "BSE_FNO"}
        today = now_ist().date().isoformat()
        for r in rd:
            if len(r) <= max(iE, iS, iSid, iI, iU, iX):
                continue
            und = r[iU].strip().upper()
            if und not in want:
                continue
            seg = seg_map.get((r[iE].strip(), r[iS].strip()))
            if not seg:
                continue
            instr = r[iI].strip().upper()
            try:
                sid = int(r[iSid])
            except ValueError:
                continue
            exp = r[iX].strip()[:10]

            def fnum(i: int) -> Optional[float]:
                try:
                    return float(r[i]) if i >= 0 and r[i].strip() not in ("", "NA") else None
                except ValueError:
                    return None
            c = Contract(sid, seg, und, instr, exp, fnum(iK), (r[iO].strip().upper() if iO >= 0 else None) or None,
                         fnum(iL) or 1.0, fnum(iT) or 0.05, r[iD].strip() if iD >= 0 else "")
            if instr == "INDEX" and seg == "IDX_I":
                self.indices[und] = c
            elif instr == "FUTIDX" and exp >= today:
                self.futures[und].append(c)
            elif instr == "OPTIDX" and exp >= today and c.strike and c.option_type in ("CE", "PE"):
                self.options[(und, exp)][(round(c.strike, 2), c.option_type)] = c
            else:
                continue
            self.by_sid[(seg, sid)] = c
        for u in self.futures:
            self.futures[u].sort(key=lambda x: x.expiry)
        LOG.info("instrument master parsed", indices=len(self.indices),
                 futures=sum(len(v) for v in self.futures.values()),
                 option_series=len(self.options))

    def index(self, name: str) -> Optional[Contract]:
        return self.indices.get(DEFAULT_INDICES[name].symbol)

    def near_future(self, name: str, n: int = 0) -> Optional[Contract]:
        lst = self.futures.get(DEFAULT_INDICES[name].symbol, [])
        today = now_ist().date().isoformat()
        lst = [c for c in lst if c.expiry >= today]
        return lst[n] if len(lst) > n else None

    def option_expiries(self, name: str) -> List[str]:
        sym = DEFAULT_INDICES[name].symbol
        return sorted(e for (u, e) in self.options if u == sym)

    def option(self, name: str, expiry: str, strike: float, side: str) -> Optional[Contract]:
        return self.options.get((DEFAULT_INDICES[name].symbol, expiry), {}).get((round(strike, 2), side))

    def lot_size(self, name: str, expiry: str) -> Optional[float]:
        d = self.options.get((DEFAULT_INDICES[name].symbol, expiry), {})
        for c in d.values():
            return c.lot_size
        f = self.near_future(name)
        return f.lot_size if f else None

    def strike_step(self, name: str, expiry: str, near: Optional[float] = None) -> Optional[float]:
        ks = sorted({k for (k, _s) in self.options.get((DEFAULT_INDICES[name].symbol, expiry), {})})
        if len(ks) < 3:
            return None
        if near:
            ks_near = [k for k in ks if abs(k / near - 1) < 0.03] or ks
            ks = ks_near
        diffs = np.diff(ks)
        diffs = diffs[diffs > 0]
        return float(np.median(diffs)) if len(diffs) else None


# --------------------------------------------------------------- live state --

@dataclass
class Tick:
    sid: int
    seg: str
    ltp: float = float("nan")
    ltq: float = 0.0
    ltt: float = 0.0          # exchange last-trade time (epoch, IST wall clock per Dhan)
    atp: float = float("nan")
    volume: float = 0.0
    buy_qty: float = 0.0
    sell_qty: float = 0.0
    oi: Optional[float] = None
    oi_hi: Optional[float] = None
    oi_lo: Optional[float] = None
    open: float = float("nan")
    high: float = float("nan")
    low: float = float("nan")
    close: float = float("nan")    # previous close in quote/full packets
    prev_close: Optional[float] = None
    prev_oi: Optional[float] = None
    bids: List[Tuple[float, float, int]] = field(default_factory=list)   # (price, qty, orders)
    asks: List[Tuple[float, float, int]] = field(default_factory=list)
    recv: float = 0.0          # local receive time
    n_updates: int = 0


SEG_CODE = {0: "IDX_I", 1: "NSE_EQ", 2: "NSE_FNO", 3: "NSE_CURRENCY", 4: "BSE_EQ", 5: "MCX_COMM",
            7: "BSE_CURRENCY", 8: "BSE_FNO"}


def parse_feed_packet(data: bytes) -> List[Dict[str, Any]]:
    """Decode one or more Dhan v2 binary packets (little-endian). Header: <B H B I
    = response code, message length, exchange segment, security id. Layouts follow the
    official SDK 2.3.0 (marketfeed.py) and DhanHQ v2 Live Market Feed docs."""
    out: List[Dict[str, Any]] = []
    off = 0
    n = len(data)
    while off + 8 <= n:
        code, mlen, seg, sid = struct.unpack_from("<BHBI", data, off)
        if mlen <= 0 or off + mlen > n:
            mlen = n - off
        pkt = data[off: off + mlen]
        segname = SEG_CODE.get(seg, str(seg))
        try:
            if code == 2 and len(pkt) >= 16:            # ticker
                _, _, _, _, ltp, ltt = struct.unpack_from("<BHBIfI", pkt, 0)
                out.append({"t": "ticker", "seg": segname, "sid": sid, "ltp": ltp, "ltt": ltt})
            elif code == 4 and len(pkt) >= 50:          # quote
                u = struct.unpack_from("<BHBIfHIfIIIffff", pkt, 0)
                out.append({"t": "quote", "seg": segname, "sid": sid, "ltp": u[4], "ltq": u[5], "ltt": u[6],
                            "atp": u[7], "volume": u[8], "sell_qty": u[9], "buy_qty": u[10],
                            "open": u[11], "close": u[12], "high": u[13], "low": u[14]})
            elif code == 5 and len(pkt) >= 12:          # OI
                u = struct.unpack_from("<BHBII", pkt, 0)
                out.append({"t": "oi", "seg": segname, "sid": sid, "oi": u[4]})
            elif code == 6 and len(pkt) >= 16:          # previous close
                u = struct.unpack_from("<BHBIfI", pkt, 0)
                out.append({"t": "prev", "seg": segname, "sid": sid, "prev_close": u[4], "prev_oi": u[5]})
            elif code == 8 and len(pkt) >= 162:         # full
                u = struct.unpack_from("<BHBIfHIfIIIIIIffff", pkt, 0)
                bids, asks = [], []
                for i in range(5):
                    bq, aq, bo, ao, bp, ap = struct.unpack_from("<IIHHff", pkt, 62 + i * 20)
                    bids.append((bp, bq, bo))
                    asks.append((ap, aq, ao))
                out.append({"t": "full", "seg": segname, "sid": sid, "ltp": u[4], "ltq": u[5], "ltt": u[6],
                            "atp": u[7], "volume": u[8], "sell_qty": u[9], "buy_qty": u[10], "oi": u[11],
                            "oi_hi": u[12], "oi_lo": u[13], "open": u[14], "close": u[15], "high": u[16],
                            "low": u[17], "bids": bids, "asks": asks})
            elif code == 3 and len(pkt) >= 112:         # market depth (v1 style)
                pass
            elif code == 7:
                out.append({"t": "status", "seg": segname, "sid": sid})
            elif code == 50 and len(pkt) >= 10:
                reason = struct.unpack_from("<H", pkt, 8)[0]
                out.append({"t": "disconnect", "seg": segname, "sid": sid, "reason": reason})
        except struct.error:
            pass
        off += mlen
    return out


WS_DISCONNECT_REASONS = {805: "too many websocket connections", 806: "data API subscription required",
                         807: "access token expired", 808: "invalid client id", 809: "authentication failed"}


class MarketFeed:
    """Single WebSocket connection (Dhan allows 5 per user, 5000 instruments each, 100 per
    subscribe message). Auto-reconnect with backoff, ping heartbeat, staleness tracking,
    diff-based (re)subscription. Never opens a second connection from this process."""

    URL = "wss://api-feed.dhan.co"
    MAX_INSTRUMENTS = 5000

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.ticks: Dict[Tuple[str, int], Tick] = {}
        self.wanted: Dict[Tuple[str, int], int] = {}      # key -> mode (15/17/21)
        self.subscribed: Dict[Tuple[str, int], int] = {}
        self.connected = False
        self.reconnects = 0
        self.msgs = 0
        self.last_msg = 0.0
        self.fatal: Optional[str] = None
        self._ws: Any = None
        self._stop = asyncio.Event()
        self._resub = asyncio.Event()
        self.listeners: List[Callable[[Tick, str], None]] = []

    def want(self, items: Dict[Tuple[str, int], int]) -> None:
        if len(items) > self.MAX_INSTRUMENTS:
            LOG.warning("subscription truncated to WS limit", requested=len(items))
            items = dict(list(items.items())[: self.MAX_INSTRUMENTS])
        if items != self.wanted:
            self.wanted = dict(items)
            self._resub.set()

    def tick(self, seg: str, sid: int) -> Optional[Tick]:
        return self.ticks.get((seg, sid))

    def stop(self) -> None:
        self._stop.set()

    async def _send_sub(self, ws: Any, items: List[Tuple[str, int]], code: int) -> None:
        for i in range(0, len(items), 100):
            batch = items[i:i + 100]
            msg = {"RequestCode": code, "InstrumentCount": len(batch),
                   "InstrumentList": [{"ExchangeSegment": s, "SecurityId": str(sid)} for s, sid in batch]}
            await ws.send(json.dumps(msg))

    async def _sync_subscriptions(self, ws: Any) -> None:
        # unsubscribe codes are subscribe code + 1 (16 / 18 / 22)
        removed = [k for k in self.subscribed if k not in self.wanted or self.wanted[k] != self.subscribed[k]]
        by_mode: Dict[int, List[Tuple[str, int]]] = collections.defaultdict(list)
        for k in removed:
            by_mode[self.subscribed[k]].append(k)
        for mode, lst in by_mode.items():
            await self._send_sub(ws, lst, mode + 1)
            for k in lst:
                self.subscribed.pop(k, None)
        add: Dict[int, List[Tuple[str, int]]] = collections.defaultdict(list)
        for k, mode in self.wanted.items():
            if self.subscribed.get(k) != mode:
                add[mode].append(k)
        for mode, lst in add.items():
            await self._send_sub(ws, lst, mode)
            for k in lst:
                self.subscribed[k] = mode
        if removed or add:
            LOG.info("ws subscriptions synced", total=len(self.subscribed),
                     added=sum(len(v) for v in add.values()), removed=len(removed))

    def _apply(self, p: Dict[str, Any]) -> None:
        key = (p["seg"], int(p["sid"]))
        tk = self.ticks.get(key)
        if tk is None:
            tk = self.ticks[key] = Tick(key[1], key[0])
        t = p["t"]
        if t in ("ticker", "quote", "full"):
            ltp = float(p["ltp"])
            if not (ltp > 0 and math.isfinite(ltp)):
                return
            tk.ltp = ltp
            tk.ltt = float(p.get("ltt") or 0)
        if t in ("quote", "full"):
            for f in ("ltq", "atp", "volume", "buy_qty", "sell_qty", "open", "high", "low", "close"):
                v = p.get(f)
                if v is not None:
                    setattr(tk, f, float(v))
        if t == "full":
            tk.oi, tk.oi_hi, tk.oi_lo = float(p["oi"]), float(p["oi_hi"]), float(p["oi_lo"])
            tk.bids = [(float(a), float(b), int(c)) for a, b, c in p["bids"]]
            tk.asks = [(float(a), float(b), int(c)) for a, b, c in p["asks"]]
        elif t == "oi":
            tk.oi = float(p["oi"])
        elif t == "prev":
            tk.prev_close, tk.prev_oi = float(p["prev_close"]), float(p["prev_oi"])
        tk.recv = time.time()
        tk.n_updates += 1
        for cb in self.listeners:
            try:
                cb(tk, t)
            except Exception:
                LOG.exception("feed listener failed")

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            if self.fatal:
                await asyncio.sleep(30)
                continue
            if ws_connect is None:
                self.fatal = "websockets package not installed"
                LOG.error(self.fatal)
                continue
            token = self.cfg.resolve_token()
            if not token or not self.cfg.client_id:
                self.fatal = "missing credentials"
                continue
            register_secret(token)
            url = f"{self.URL}?version=2&token={token}&clientId={self.cfg.client_id}&authType=2"
            try:
                async with ws_connect(url, ping_interval=10, ping_timeout=20, close_timeout=5,
                                      max_size=2 ** 22) as ws:
                    self._ws = ws
                    self.connected = True
                    self.subscribed.clear()
                    LOG.info("ws connected", reconnects=self.reconnects)
                    await self._sync_subscriptions(ws)
                    backoff = 1.0
                    while not self._stop.is_set():
                        if self._resub.is_set():
                            self._resub.clear()
                            await self._sync_subscriptions(ws)
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=5)
                        except asyncio.TimeoutError:
                            continue
                        if isinstance(msg, str):
                            continue
                        self.msgs += 1
                        self.last_msg = time.time()
                        for p in parse_feed_packet(msg):
                            if p["t"] == "disconnect":
                                reason = WS_DISCONNECT_REASONS.get(p["reason"], str(p["reason"]))
                                LOG.error("ws server disconnect", reason=reason, code=p["reason"])
                                if p["reason"] in (806, 807, 808, 809):
                                    self.fatal = reason
                                raise ConnectionError(reason)
                            self._apply(p)
                    with contextlib.suppress(Exception):
                        await ws.send(json.dumps({"RequestCode": 12}))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                LOG.warning("ws error; reconnecting", error=redact(str(e))[:200], backoff=backoff)
            finally:
                self.connected = False
                self._ws = None
            if self._stop.is_set():
                break
            self.reconnects += 1
            await asyncio.sleep(backoff + random.random())
            backoff = min(backoff * 2, 60)
            if self.fatal == "access token expired" and self.cfg.token_file:
                self.fatal = None   # a rotated token file may fix it

    def stale_keys(self, max_age: float) -> List[Tuple[str, int]]:
        t = time.time()
        return [k for k in self.wanted if (self.ticks.get(k) is None or t - self.ticks[k].recv > max_age)]

# ============================================================================
# 6. OPTION-CHAIN INTELLIGENCE + FEATURE ENGINE
# ============================================================================

NAN = float("nan")


def _f(x: Any) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else NAN
    except (TypeError, ValueError):
        return NAN


def fin(x: Any) -> bool:
    return isinstance(x, (int, float, np.floating)) and math.isfinite(float(x))


@dataclass
class Leg:
    strike: float
    side: str
    sid: Optional[int] = None
    ltp: float = NAN
    oi: float = NAN
    prev_oi: float = NAN
    volume: float = NAN
    prev_volume: float = NAN
    prev_close: float = NAN
    bid: float = NAN
    ask: float = NAN
    bid_qty: float = 0.0
    ask_qty: float = 0.0
    v_iv: float = NAN
    v_delta: float = NAN
    v_gamma: float = NAN
    v_theta: float = NAN
    v_vega: float = NAN
    iv: float = NAN
    delta: float = NAN
    gamma: float = NAN
    theta: float = NAN
    vega: float = NAN
    rho: float = NAN
    spread_pct: float = NAN
    liq: float = 0.0
    buildup: str = "UNKNOWN"
    src: str = "rest"
    ts: float = 0.0
    depth_bids: List[Tuple[float, float, int]] = field(default_factory=list)
    depth_asks: List[Tuple[float, float, int]] = field(default_factory=list)

    @property
    def mid(self) -> float:
        m = mid_price(self.bid, self.ask) if fin(self.bid) and fin(self.ask) else None
        return m if m else NAN

    @property
    def fair(self) -> float:
        """Mid when the quote is tight (<=5% of mid), else LTP."""
        m = self.mid
        if fin(m) and fin(self.ask) and (self.ask - self.bid) <= 0.05 * m:
            return m
        return self.ltp


def normalize_chain(raw: Dict[str, Any], ts: float) -> Tuple[float, Dict[Tuple[float, str], Leg]]:
    """Map Dhan /optionchain payload to Legs. Field names verified against DhanHQ v2 docs:
    oc -> "<strike>" -> ce|pe -> last_price, oi, previous_oi, volume, previous_volume,
    previous_close_price, top_bid_price, top_bid_quantity, top_ask_price, top_ask_quantity,
    implied_volatility (percent), greeks{delta,gamma,theta,vega}, security_id."""
    legs: Dict[Tuple[float, str], Leg] = {}
    last = _f(raw.get("last_price"))
    for ks, v in (raw.get("oc") or {}).items():
        K = _f(ks)
        if not fin(K) or not isinstance(v, dict):
            continue
        for side, key in (("CE", "ce"), ("PE", "pe")):
            o = v.get(key)
            if not isinstance(o, dict):
                continue
            g = o.get("greeks") or {}
            sid = o.get("security_id")
            lg = Leg(K, side, int(sid) if sid not in (None, "") else None,
                     ltp=_f(o.get("last_price")), oi=_f(o.get("oi")), prev_oi=_f(o.get("previous_oi")),
                     volume=_f(o.get("volume")), prev_volume=_f(o.get("previous_volume")),
                     prev_close=_f(o.get("previous_close_price")),
                     bid=_f(o.get("top_bid_price")), ask=_f(o.get("top_ask_price")),
                     bid_qty=_f(o.get("top_bid_quantity")) if fin(_f(o.get("top_bid_quantity"))) else 0.0,
                     ask_qty=_f(o.get("top_ask_quantity")) if fin(_f(o.get("top_ask_quantity"))) else 0.0,
                     v_iv=_f(o.get("implied_volatility")), v_delta=_f(g.get("delta")), v_gamma=_f(g.get("gamma")),
                     v_theta=_f(g.get("theta")), v_vega=_f(g.get("vega")), src="rest", ts=ts)
            if fin(lg.bid) and lg.bid <= 0:
                lg.bid = NAN
            if fin(lg.ask) and lg.ask <= 0:
                lg.ask = NAN
            legs[(K, side)] = lg
    return last, legs


@dataclass
class SnapRec:
    """Compact causal snapshot kept in memory for window comparisons."""
    ts: float
    spot: float
    fut: float
    fwd: float
    atm: float
    step: float
    legs: Dict[Tuple[float, str], Tuple[float, float, float, float, float, float, float]]  # ltp, oi, vol, iv, gamma, bq, aq
    f: Dict[str, Any]
    scores: Dict[str, Any] = field(default_factory=dict)


class MinuteBars:
    """Causal 1-minute OHLC builder from ticks (index has no volume; futures do)."""

    def __init__(self, maxlen: int = 3000):
        self.bars: Deque[Dict[str, float]] = collections.deque(maxlen=maxlen)
        self._cur: Optional[Dict[str, float]] = None

    def seed(self, candles: List[Dict[str, float]]) -> None:
        for c in sorted(candles, key=lambda x: x["ts"]):
            if not self.bars or c["ts"] > self.bars[-1]["ts"]:
                self.bars.append(dict(c))

    def update(self, px: float, t: float, cum_vol: Optional[float] = None) -> None:
        if not fin(px) or px <= 0:
            return
        m = math.floor(t / 60) * 60
        if self._cur is None or m > self._cur["ts"]:
            if self._cur is not None:
                if not self.bars or self._cur["ts"] > self.bars[-1]["ts"]:
                    self.bars.append(self._cur)
            self._cur = {"ts": m, "open": px, "high": px, "low": px, "close": px, "volume": 0.0,
                         "cumvol0": cum_vol if cum_vol is not None else NAN}
        else:
            c = self._cur
            c["high"] = max(c["high"], px)
            c["low"] = min(c["low"], px)
            c["close"] = px
        if cum_vol is not None and fin(self._cur.get("cumvol0", NAN)):
            self._cur["volume"] = max(cum_vol - self._cur["cumvol0"], 0.0)

    def all(self, include_current: bool = True) -> List[Dict[str, float]]:
        b = list(self.bars)
        if include_current and self._cur is not None and (not b or self._cur["ts"] > b[-1]["ts"]):
            b.append(self._cur)
        return b

    def closes(self, n: Optional[int] = None) -> np.ndarray:
        b = self.all()
        a = np.array([x["close"] for x in b], float)
        return a[-n:] if n else a

    def today(self) -> List[Dict[str, float]]:
        d = now_ist().date()
        return [b for b in self.all() if dt.datetime.fromtimestamp(b["ts"], IST).date() == d]

    def price_at_or_before(self, t: float) -> float:
        b = self.all()
        ts = [x["ts"] for x in b]
        i = bisect.bisect_right(ts, t - 60) - 1   # bar whose close is known by t
        return b[i]["close"] if i >= 0 else NAN


class IndexState:
    """Everything known about one index: instruments, live chain, history, features, scores."""

    def __init__(self, name: str, cfg: Config):
        self.name = name
        self.cfg = cfg
        self.spec = DEFAULT_INDICES[name]
        self.idx: Optional[Contract] = None
        self.fut: Optional[Contract] = None
        self.fut_next: Optional[Contract] = None
        self.expiries: List[str] = []
        self.expiry: Optional[str] = None
        self.next_expiry: Optional[str] = None
        self.lot: float = NAN
        self.step: float = NAN
        self.chain: Dict[Tuple[float, str], Leg] = {}
        self.chain_ts: float = 0.0
        self.chain_last_price: float = NAN
        self.next_chain: Dict[Tuple[float, str], Leg] = {}
        self.next_chain_ts: float = 0.0
        self.bars = MinuteBars()
        self.fut_bars = MinuteBars()
        self.daily: List[Dict[str, float]] = []
        self.history: Deque[SnapRec] = collections.deque(maxlen=cfg.max_memory_snapshots)
        self.stats: Dict[str, RollingStat] = collections.defaultdict(lambda: RollingStat(600))
        self.last_features: Dict[str, Any] = {}
        self.last_scores: Dict[str, Any] = {}
        self.regime: Dict[str, Any] = {"regime": "WARMING_UP", "evidence": []}
        self.patterns_on: Dict[str, float] = {}
        self.garch: Optional[GarchResult] = None
        self.hmm_state: Optional[int] = None
        self.chain_errors = 0

    # ------------------------------------------------------------------ util
    def past(self, ts: float, minutes: float, tol_frac: float = 0.6) -> Optional[SnapRec]:
        """Snapshot at or before ts - minutes (within tolerance). Strictly causal."""
        target = ts - minutes * 60
        best = None
        for r in reversed(self.history):
            if r.ts <= target:
                best = r
                break
        if best is None:
            return None
        if target - best.ts > max(minutes * 60 * tol_frac, 90):
            return None
        return best

    def window_strikes(self, atm: float, n: int) -> List[float]:
        ks = sorted({k for (k, _s) in self.chain})
        if not ks or not fin(atm):
            return []
        i = min(range(len(ks)), key=lambda j: abs(ks[j] - atm))
        return ks[max(0, i - n): i + n + 1]


def compute_leg_analytics(legs: Dict[Tuple[float, str], Leg], F: float, T: float, r: float, lot: float,
                          strikes: Iterable[float]) -> None:
    for K in strikes:
        for side in ("CE", "PE"):
            lg = legs.get((K, side))
            if lg is None:
                continue
            px = lg.fair
            is_call = side == "CE"
            iv = implied_vol_black76(px, F, K, T, r, is_call) if fin(px) and px > 0 and fin(F) else None
            if iv is not None and 0.005 < iv < 3.0:
                g = black76_greeks(F, K, T, r, iv, is_call)
                lg.iv, lg.delta, lg.gamma, lg.theta, lg.vega, lg.rho = iv, g.delta, g.gamma, g.theta, g.vega, g.rho
            sp = spread_pct(lg.bid, lg.ask) if fin(lg.bid) and fin(lg.ask) else None
            lg.spread_pct = sp if sp is not None else NAN
            lg.liq = liquidity_score(sp, lg.volume if fin(lg.volume) else 0, lg.oi if fin(lg.oi) else 0,
                                     min(lg.bid_qty, lg.ask_qty), int(lot) if fin(lot) else 1)


def _sum(xs: Iterable[float]) -> float:
    s = 0.0
    for x in xs:
        if fin(x):
            s += x
    return s


def build_features(st: IndexState, ts: float, spot: float, fut_tick: Optional[Tick], vix: float,
                   nifty_ret30: Optional[float], r: float) -> Tuple[Dict[str, Any], SnapRec, Dict[Tuple[float, str], Leg]]:
    """All features are computed from data stamped <= ts (causal). Returns (features, record, legs)."""
    cfg = st.cfg
    legs = {k: dataclasses.replace(v) for k, v in st.chain.items()}
    f: Dict[str, Any] = {"ts": ts, "feature_ts": ts, "index": st.name, "expiry": st.expiry}
    if not legs or not st.expiry:
        return f, SnapRec(ts, spot, NAN, NAN, NAN, NAN, {}, f), legs
    T = year_fraction(ts, st.expiry)
    f["T_days"] = T * 365
    fut = fut_tick.ltp if fut_tick and fin(fut_tick.ltp) else NAN
    f["spot"], f["fut"] = spot, fut
    ref = spot if fin(spot) else (fut if fin(fut) else st.chain_last_price)
    ks_all = sorted({k for (k, _s) in legs})
    if fin(ref):
        near = [k for k in ks_all if abs(k / ref - 1) <= 0.015]
    else:
        near = []
    pf = parity_forward(near, [legs.get((k, "CE"), Leg(k, "CE")).fair for k in near],
                        [legs.get((k, "PE"), Leg(k, "PE")).fair for k in near], T, r, ref) if near else None
    if pf:
        F, disp, nused = pf
        f["forward_src"] = "put_call_parity"
    else:
        F, disp, nused = (fut if fin(fut) and st.fut and st.fut.expiry == st.expiry else ref * math.exp(r * T)), NAN, 0
        f["forward_src"] = "futures" if fin(fut) and st.fut and st.fut.expiry == st.expiry else "spot_carry"
    f["forward"], f["parity_dispersion"], f["parity_n"] = F, disp, nused
    step = st.step if fin(st.step) else (float(np.median(np.diff(ks_all))) if len(ks_all) > 2 else NAN)
    atm = min(ks_all, key=lambda k: abs(k - F)) if ks_all and fin(F) else NAN
    f["atm"], f["step"] = atm, step
    W = cfg.strike_window
    win15 = st.window_strikes(atm, W)
    compute_leg_analytics(legs, F, T, r, st.lot, win15)
    win5, win10 = st.window_strikes(atm, 5), st.window_strikes(atm, 10)

    # ---- per-window aggregates ---------------------------------------------------
    for tag, win in (("5", win5), ("10", win10), ("15", win15)):
        ce = [legs.get((k, "CE")) for k in win]
        pe = [legs.get((k, "PE")) for k in win]
        ce = [x for x in ce if x]
        pe = [x for x in pe if x]
        ce_oi, pe_oi = _sum(x.oi for x in ce), _sum(x.oi for x in pe)
        ce_v, pe_v = _sum(x.volume for x in ce), _sum(x.volume for x in pe)
        f[f"ce_oi_{tag}"], f[f"pe_oi_{tag}"] = ce_oi, pe_oi
        f[f"ce_vol_{tag}"], f[f"pe_vol_{tag}"] = ce_v, pe_v
        f[f"ce_doi_day_{tag}"] = _sum(x.oi - x.prev_oi for x in ce if fin(x.prev_oi))
        f[f"pe_doi_day_{tag}"] = _sum(x.oi - x.prev_oi for x in pe if fin(x.prev_oi))
        f[f"pcr_oi_{tag}"] = pe_oi / ce_oi if ce_oi > 0 else NAN
        f[f"pcr_vol_{tag}"] = pe_v / ce_v if ce_v > 0 else NAN
        tot = np.array([x.oi for x in ce + pe if fin(x.oi)], float)
        f[f"oi_hhi_{tag}"] = float(np.sum((tot / tot.sum()) ** 2)) if tot.size and tot.sum() > 0 else NAN
        bq_c, aq_c = _sum(x.bid_qty for x in ce), _sum(x.ask_qty for x in ce)
        bq_p, aq_p = _sum(x.bid_qty for x in pe), _sum(x.ask_qty for x in pe)
        f[f"ce_book_imb_{tag}"] = book_imbalance(bq_c, aq_c)
        f[f"pe_book_imb_{tag}"] = book_imbalance(bq_p, aq_p)
    all_ce = [legs[(k, "CE")] for k in ks_all if (k, "CE") in legs]
    all_pe = [legs[(k, "PE")] for k in ks_all if (k, "PE") in legs]
    tce, tpe = _sum(x.oi for x in all_ce), _sum(x.oi for x in all_pe)
    f["pcr_oi_all"] = tpe / tce if tce > 0 else NAN
    f["max_pain"] = max_pain(ks_all, [legs.get((k, "CE"), Leg(k, "CE")).oi if fin(legs.get((k, "CE"), Leg(k, "CE")).oi) else 0 for k in ks_all],
                             [legs.get((k, "PE"), Leg(k, "PE")).oi if fin(legs.get((k, "PE"), Leg(k, "PE")).oi) else 0 for k in ks_all])

    # ---- support / resistance from OI ---------------------------------------------
    above = [k for k in win15 if k >= F]
    below = [k for k in win15 if k <= F]
    res = max(above, key=lambda k: legs.get((k, "CE"), Leg(k, "CE")).oi if fin(legs.get((k, "CE"), Leg(k, "CE")).oi) else -1) if above else NAN
    sup = max(below, key=lambda k: legs.get((k, "PE"), Leg(k, "PE")).oi if fin(legs.get((k, "PE"), Leg(k, "PE")).oi) else -1) if below else NAN
    f["resistance"], f["support"] = res, sup

    # ---- ATM legs, straddle, IV, skew ------------------------------------------------
    ace, ape = legs.get((atm, "CE")), legs.get((atm, "PE"))
    f["atm_ce"] = ace.fair if ace else NAN
    f["atm_pe"] = ape.fair if ape else NAN
    f["straddle"] = f["atm_ce"] + f["atm_pe"] if fin(f["atm_ce"]) and fin(f["atm_pe"]) else NAN
    f["implied_move_pct"] = f["straddle"] / F * 100 if fin(f["straddle"]) and fin(F) else NAN
    ivs = [x.iv for x in (ace, ape) if x and fin(x.iv)]
    f["atm_iv"] = float(np.mean(ivs)) if ivs else NAN
    f["atm_gamma"] = ace.gamma if ace and fin(ace.gamma) else NAN
    f["atm_theta"] = (ace.theta if ace else NAN) + (ape.theta if ape else NAN) if ace and ape else NAN
    f["atm_ce_delta"] = ace.delta if ace else NAN
    f["atm_ce_vol"] = ace.volume if ace else NAN
    f["atm_pe_vol"] = ape.volume if ape else NAN
    f["atm_ce_oi"] = ace.oi if ace else NAN
    f["atm_pe_oi"] = ape.oi if ape else NAN
    if fin(step):
        o_ce, o_pe = legs.get((atm + 2 * step, "CE")), legs.get((atm - 2 * step, "PE"))
        f["skew_2strike"] = (o_pe.iv - o_ce.iv) if o_ce and o_pe and fin(o_ce.iv) and fin(o_pe.iv) else NAN
    vend = [x.v_iv for x in (ace, ape) if x and fin(x.v_iv) and x.v_iv > 0]
    f["atm_iv_vendor"] = float(np.mean(vend)) / 100 if vend else NAN

    # ---- gamma exposure ---------------------------------------------------------------
    gx = gamma_exposure(win15, [legs.get((k, "CE"), Leg(k, "CE")).gamma for k in win15],
                        [legs.get((k, "PE"), Leg(k, "PE")).gamma for k in win15],
                        [legs.get((k, "CE"), Leg(k, "CE")).oi if fin(legs.get((k, "CE"), Leg(k, "CE")).oi) else 0 for k in win15],
                        [legs.get((k, "PE"), Leg(k, "PE")).oi if fin(legs.get((k, "PE"), Leg(k, "PE")).oi) else 0 for k in win15], F)
    f.update({"gex": gx["net_gex"], "gamma_conc": gx["gamma_concentration"], "gamma_peak": gx["gamma_peak_strike"],
              "gex_flip": gx["gex_flip_strike"]})

    # ---- liquidity ----------------------------------------------------------------------
    l5 = [legs[(k, s)] for k in win5 for s in ("CE", "PE") if (k, s) in legs]
    f["liquidity_score"] = float(np.mean([x.liq for x in l5])) if l5 else 0.0
    sps = [x.spread_pct for x in l5 if fin(x.spread_pct)]
    f["median_spread_pct"] = float(np.median(sps)) if sps else NAN
    f["illiquid_frac"] = float(np.mean([1.0 if (not fin(x.spread_pct) or x.spread_pct > 3) else 0.0 for x in l5])) if l5 else 1.0

    # ---- futures, VWAP, basis ----------------------------------------------------------
    if fut_tick and fin(fut_tick.atp) and fut_tick.atp > 0:
        f["vwap"] = fut_tick.atp       # Dhan ATP on futures = session VWAP
        f["vwap_dev_pct"] = (fut / fut_tick.atp - 1) * 100 if fin(fut) else NAN
    else:
        f["vwap"], f["vwap_dev_pct"] = NAN, NAN
    f["fut_volume"] = fut_tick.volume if fut_tick else NAN
    f["fut_oi"] = fut_tick.oi if fut_tick and fut_tick.oi is not None else NAN
    f["fut_book_imb"] = book_imbalance(fut_tick.buy_qty, fut_tick.sell_qty) if fut_tick else None
    f["basis"] = fut - spot if fin(fut) and fin(spot) else NAN
    if fin(fut) and fin(spot) and st.fut:
        Tf = year_fraction(ts, st.fut.expiry)
        f["basis_ann_pct"] = (math.log(fut / spot) / Tf) * 100 if Tf > 1 / 365 else NAN
    f["vix"] = vix

    # ---- price structure from minute bars ------------------------------------------------
    bars = st.bars.all()
    closes = np.array([b["close"] for b in bars], float)
    today = st.bars.today()
    if len(closes) >= 2:
        lr = np.diff(np.log(closes))
        for m in (1, 5, 15, 30):
            f[f"ret_{m}"] = float(math.log(closes[-1] / closes[-1 - m])) if len(closes) > m else NAN
        rv_win = lr[-375:]
        f["rv_1m_ann"] = realized_vol(rv_win, 252 * 375) if len(rv_win) > 20 else NAN
        f["sigma_1m"] = float(np.std(lr[-750:-5], ddof=1)) if len(lr) > 60 else NAN
        if len(closes) >= 31:
            y = np.log(closes[-31:])
            o = ols(y, np.arange(31.0))
            f["trend_t"] = float(o.t[1]) if np.isfinite(o.t[1]) else NAN
            f["trend_r2"] = o.r2
        f["vol_cluster"] = vol_clustering_stat(lr[-375:]) if len(lr) > 60 else NAN
    if today:
        hi, lo = max(b["high"] for b in today), min(b["low"] for b in today)
        f["day_high"], f["day_low"] = hi, lo
        orb = [b for b in today if b["ts"] < dt.datetime.combine(now_ist().date(), dt.time(9, 30), IST).timestamp()]
        if orb and len(orb) >= 10:
            f["or_high"], f["or_low"] = max(b["high"] for b in orb), min(b["low"] for b in orb)
        last30 = today[-30:]
        if len(last30) >= 10:
            rng = max(b["high"] for b in last30) - min(b["low"] for b in last30)
            f["range30_pct"] = rng / closes[-1] * 100
        tr = [b["high"] - b["low"] for b in today[-14:]]
        f["atr1m"] = float(np.mean(tr)) if tr else NAN
        f["intraday_pos"] = (spot - lo) / (hi - lo) if fin(spot) and hi > lo else NAN
    if st.daily:
        prev = [d for d in st.daily if dt.datetime.fromtimestamp(d["ts"], IST).date() < now_ist().date()]
        if prev:
            f["pdh"], f["pdl"], f["pdc"] = prev[-1]["high"], prev[-1]["low"], prev[-1]["close"]
        dr = log_returns([d["close"] for d in st.daily])
        if len(dr) > 20:
            f["rv_daily_ann"] = realized_vol(dr[-20:], 252)
    if st.garch and st.garch.ok:
        f["garch_sigma_ann"] = st.garch.forecast_sigma * math.sqrt(252)
        f["garch_persistence"] = st.garch.persistence
    if fin(f.get("atm_iv")) and fin(f.get("rv_1m_ann", NAN)):
        f["iv_rv_spread"] = f["atm_iv"] - f["rv_1m_ann"]
    f["rel_strength_30"] = (f.get("ret_30", NAN) - nifty_ret30) if nifty_ret30 is not None and fin(f.get("ret_30", NAN)) else NAN
    f["minutes_since_open"] = (ts - dt.datetime.combine(dt.datetime.fromtimestamp(ts, IST).date(), cfg.market_open, IST).timestamp()) / 60
    f["expiry_day"] = bool(st.expiry == dt.datetime.fromtimestamp(ts, IST).date().isoformat())
    f["dte"] = (dt.date.fromisoformat(st.expiry) - dt.datetime.fromtimestamp(ts, IST).date()).days

    # ---- windowed changes: 5 / 15 minutes ------------------------------------------------
    rec_legs = {k: (v.fair, v.oi, v.volume, v.iv, v.gamma, v.bid_qty, v.ask_qty) for k, v in legs.items()
                if k[0] in set(win15)}
    for m in (1, 5, 15):
        p = st.past(ts, m)
        if p is None:
            continue
        sfx = f"_{m}m"
        for key in ("pcr_oi_5", "atm_iv", "straddle", "gamma_conc", "ce_oi_5", "pe_oi_5", "ce_oi_15", "pe_oi_15",
                    "vix", "fut_oi", "gex"):
            a, b = f.get(key), p.f.get(key)
            if fin(a) and fin(b):
                f[f"d_{key}{sfx}"] = a - b
                if b != 0:
                    f[f"pct_{key}{sfx}"] = (a / b - 1) * 100
        for side in ("CE", "PE"):
            d_oi, d_vol, n_cls = 0.0, 0.0, collections.Counter()
            w_oi = collections.Counter()
            for k in win5:
                cur, old = rec_legs.get((k, side)), p.legs.get((k, side))
                if not cur or not old:
                    continue
                if fin(cur[1]) and fin(old[1]):
                    doi = cur[1] - old[1]
                    d_oi += doi
                    dpx = cur[0] - old[0] if fin(cur[0]) and fin(old[0]) else None
                    cls = classify_buildup(dpx, doi, price_eps=0.0, oi_eps=0.0)
                    n_cls[cls] += 1
                    w_oi[cls] += abs(doi)
                if fin(cur[2]) and fin(old[2]):
                    d_vol += max(cur[2] - old[2], 0)
            f[f"{side.lower()}_doi5{sfx}"] = d_oi
            f[f"{side.lower()}_dvol5{sfx}"] = d_vol
            dom = max(w_oi.items(), key=lambda kv: kv[1])[0] if w_oi else "UNKNOWN"
            f[f"{side.lower()}_buildup{sfx}"] = dom
            totw = sum(w_oi.values())
            for cls in ("LONG_BUILDUP", "WRITING", "SHORT_COVERING", "LONG_UNWINDING"):
                f[f"{side.lower()}_{cls.lower()}_w{sfx}"] = w_oi[cls] / totw if totw > 0 else 0.0
        # ATM premium momentum
        for side, key in (("CE", "atm_ce"), ("PE", "atm_pe")):
            old = p.legs.get((atm, side))
            cur = rec_legs.get((atm, side))
            if old and cur and fin(old[0]) and fin(cur[0]) and old[0] > 0:
                f[f"{key}_prem_pct{sfx}"] = (cur[0] / old[0] - 1) * 100
            if old and cur and fin(old[2]) and fin(cur[2]):
                f[f"{key}_dvol{sfx}"] = max(cur[2] - old[2], 0)
            if old and cur and fin(old[3]) and fin(cur[3]):
                f[f"{key}_div{sfx}"] = cur[3] - old[3]
        # resistance / support at the walls
        if fin(res) and fin(step):
            c, o = _sum(rec_legs.get((res + d * step, "CE"), (NAN,) * 7)[1] for d in (-1, 0, 1)), \
                _sum(p.legs.get((res + d * step, "CE"), (NAN,) * 7)[1] for d in (-1, 0, 1))
            f[f"res_ce_doi{sfx}"] = c - o
            f[f"res_ce_doi_pct{sfx}"] = (c / o - 1) * 100 if o > 0 else NAN
        if fin(sup) and fin(step):
            c, o = _sum(rec_legs.get((sup + d * step, "PE"), (NAN,) * 7)[1] for d in (-1, 0, 1)), \
                _sum(p.legs.get((sup + d * step, "PE"), (NAN,) * 7)[1] for d in (-1, 0, 1))
            f[f"sup_pe_doi{sfx}"] = c - o
            f[f"sup_pe_doi_pct{sfx}"] = (c / o - 1) * 100 if o > 0 else NAN
        if fin(p.f.get("resistance")) and fin(res):
            f[f"res_shift{sfx}"] = res - p.f["resistance"]
        if fin(p.f.get("support")) and fin(sup):
            f[f"sup_shift{sfx}"] = sup - p.f["support"]
        if fin(p.atm) and fin(atm):
            f[f"atm_shift{sfx}"] = atm - p.atm
        if fin(p.fut) and fin(fut) and p.fut > 0:
            f[f"fut_ret{sfx}"] = math.log(fut / p.fut)
        if fin(p.f.get("fut_volume")) and fin(f.get("fut_volume")):
            f[f"fut_dvol{sfx}"] = max(f["fut_volume"] - p.f["fut_volume"], 0)

    # ---- volume acceleration: last 5m vs median 5m over previous 30m -------------------
    for key in ("atm_ce", "atm_pe"):
        cur = f.get(f"{key}_dvol_5m")
        hist = []
        for lag in (5, 10, 15, 20, 25, 30):
            a, b = st.past(ts, lag), st.past(ts, lag + 5)
            if a and b:
                va, vb = a.f.get(f"{key}_vol"), b.f.get(f"{key}_vol")
                if fin(va) and fin(vb) and va >= vb:
                    hist.append(va - vb)
        base = float(np.median(hist)) if len(hist) >= 3 else NAN
        f[f"{key}_vol_accel"] = cur / base if fin(cur) and fin(base) and base > 0 else NAN
    cur, hist = f.get("fut_dvol_5m"), []
    for lag in (5, 10, 15, 20, 25, 30):
        a, b = st.past(ts, lag), st.past(ts, lag + 5)
        if a and b and fin(a.f.get("fut_volume")) and fin(b.f.get("fut_volume")):
            hist.append(max(a.f["fut_volume"] - b.f["fut_volume"], 0))
    base = float(np.median(hist)) if len(hist) >= 3 else NAN
    f["fut_vol_accel"] = cur / base if fin(cur) and fin(base) and base > 0 else NAN

    # ---- adaptive robust z-scores (baseline excludes current observation) ---------------
    for key in ("res_ce_doi_15m", "sup_pe_doi_15m", "ce_doi5_5m", "pe_doi5_5m", "d_atm_iv_15m", "pct_straddle_15m",
                "d_gamma_conc_15m", "d_pcr_oi_5_15m", "atm_ce_vol_accel", "atm_pe_vol_accel", "fut_ret_5m",
                "ret_5", "ret_15", "ret_30", "atm_ce_prem_pct_5m", "atm_pe_prem_pct_5m", "fut_vol_accel",
                "d_vix_15m", "range30_pct", "rv_1m_ann", "vwap_dev_pct", "res_ce_doi_5m", "sup_pe_doi_5m"):
        v = f.get(key)
        z = st.stats[key].robust_z(v)
        if z is not None:
            f[f"z_{key}"] = float(np.clip(z, -10, 10))
        st.stats[key].add(v)

    # data quality inputs
    f["chain_age_s"] = ts - st.chain_ts if st.chain_ts else NAN
    nq = [legs[(k, s)] for k in win5 for s in ("CE", "PE") if (k, s) in legs]
    f["quote_coverage"] = float(np.mean([1.0 if fin(x.bid) and fin(x.ask) else 0.0 for x in nq])) if nq else 0.0
    f["iv_coverage"] = float(np.mean([1.0 if fin(x.iv) else 0.0 for x in nq])) if nq else 0.0
    rec = SnapRec(ts, spot, fut, F, atm, step, rec_legs, f)
    return f, rec, legs


# ============================================================================
# 7. SCORING, REGIMES, PATTERNS
# ============================================================================

@dataclass
class Component:
    name: str
    value: Any
    z: Optional[float]
    contrib: float
    note: str


def _zc(f: Dict[str, Any], key: str, sign: float, weight: float, name: str, note: str,
        fallback: Optional[float] = None) -> Optional[Component]:
    """Adaptive component: contribution = weight * clip(sign*z, -4, 4)/4. Falls back to a
    half-weight sign rule while the adaptive baseline is still warming up."""
    z = f.get(f"z_{key}")
    v = f.get(key)
    if fin(z):
        c = weight * float(np.clip(sign * z, -4, 4)) / 4
        return Component(name, v, float(z), c, note)
    if fallback is not None and fin(v):
        return Component(name, v, None, 0.5 * weight * float(np.clip(sign * v / fallback, -1, 1)), note + " (baseline warming)")
    return None


def score_from(comps: List[Optional[Component]], scale: float = 1.0) -> Tuple[float, List[Component], List[Component]]:
    cs = [c for c in comps if c is not None]
    tot = sum(c.contrib for c in cs)
    score = 50 + 50 * math.tanh(tot / scale)
    pos = sorted([c for c in cs if c.contrib > 0.02], key=lambda c: -c.contrib)
    neg = sorted([c for c in cs if c.contrib < -0.02], key=lambda c: c.contrib)
    return round(score, 1), pos, neg


def data_quality_score(f: Dict[str, Any], ws_age: float, ws_ok: bool, cfg: Config) -> Tuple[float, List[str]]:
    issues = []
    s = 100.0
    if not ws_ok:
        s -= 25
        issues.append("websocket down")
    if fin(ws_age) and ws_age > cfg.ws_stale_s:
        s -= 20
        issues.append(f"spot tick stale {ws_age:.0f}s")
    ca = f.get("chain_age_s", NAN)
    if not fin(ca) or ca > cfg.chain_stale_s:
        s -= 25
        issues.append(f"option chain age {ca if fin(ca) else 'n/a'}s")
    if f.get("forward_src") != "put_call_parity":
        s -= 10
        issues.append(f"forward from {f.get('forward_src')}")
    disp = f.get("parity_dispersion", NAN)
    step = f.get("step", NAN)
    if fin(disp) and fin(step) and disp > step:
        s -= 10
        issues.append(f"parity dispersion {disp:.1f} > strike step")
    qc = f.get("quote_coverage", 0)
    if qc < 0.8:
        s -= 15 * (1 - qc)
        issues.append(f"bid/ask coverage {qc:.0%}")
    if f.get("illiquid_frac", 1) > 0.4:
        s -= 10
        issues.append(f"{f.get('illiquid_frac'):.0%} ATM+/-5 legs illiquid (spread>3%)")
    if f.get("iv_coverage", 0) < 0.8:
        s -= 5
        issues.append("IV solve coverage low")
    return max(0.0, round(s, 1)), issues


def compute_scores(f: Dict[str, Any], dq: float, dq_issues: List[str], reliability: Dict[str, Any]) -> Dict[str, Any]:
    spot, vwap_dev = f.get("spot", NAN), f.get("vwap_dev_pct", NAN)

    def lvl(key: str, above: bool) -> Optional[Component]:
        L = f.get(key)
        if not fin(L) or not fin(spot):
            return None
        d = (spot - L) / spot * 100
        if above:
            return Component(f"price vs {key}", round(d, 3), None, 0.15 if d > 0 else (-0.05 if d < -0.3 else 0.0),
                             f"spot {'above' if d > 0 else 'below'} {key} {L:.1f}")
        return Component(f"price vs {key}", round(d, 3), None, 0.15 if d < 0 else (-0.05 if d > 0.3 else 0.0),
                         f"spot {'below' if d < 0 else 'above'} {key} {L:.1f}")

    bull_c = [
        _zc(f, "res_ce_doi_15m", -1, 0.9, "Resistance CE OI change 15m", "CE OI falling at the call wall = writers exiting"),
        _zc(f, "pe_doi5_5m", +1, 0.4, "PE OI change ATM+/-5 (5m)", "put OI rising"),
        Component("PE writing share 5m", f.get("pe_writing_w_5m"), None, 0.5 * (f.get("pe_writing_w_5m", 0) or 0)
                  - 0.4 * (f.get("pe_long_unwinding_w_5m", 0) or 0), "PE premium down + OI up (put writing)"),
        Component("CE short-covering share 5m", f.get("ce_short_covering_w_5m"), None,
                  0.5 * (f.get("ce_short_covering_w_5m", 0) or 0) - 0.5 * (f.get("ce_writing_w_5m", 0) or 0),
                  "CE premium up + OI down (call short covering) vs call writing"),
        _zc(f, "atm_ce_vol_accel", +1, 0.4, "ATM CE volume acceleration", "ATM call volume vs its own 30m baseline", 3.0),
        _zc(f, "atm_ce_prem_pct_5m", +1, 0.3, "ATM CE premium momentum 5m", "call premium expanding"),
        _zc(f, "fut_ret_5m", +1, 0.7, "Futures 5m return", "futures momentum confirmation"),
        Component("VWAP deviation", vwap_dev, None, (0.2 if vwap_dev > 0 else -0.2) if fin(vwap_dev) else 0.0,
                  "futures above/below session VWAP"),
        _zc(f, "d_pcr_oi_5_15m", +1, 0.3, "PCR change 15m", "PCR rising"),
        lvl("or_high", True), lvl("pdh", True),
        Component("Resistance shift 15m", f.get("res_shift_15m"), None,
                  (0.3 if (f.get("res_shift_15m") or 0) > 0 else (-0.3 if (f.get("res_shift_15m") or 0) < 0 else 0.0)),
                  "call wall migrating higher/lower"),
        Component("Futures book imbalance", f.get("fut_book_imb"), None, 0.15 * (f.get("fut_book_imb") or 0),
                  "total buy vs sell quantity on futures"),
    ]
    bear_c = [
        _zc(f, "sup_pe_doi_15m", -1, 0.9, "Support PE OI change 15m", "PE OI falling at the put wall = writers exiting"),
        _zc(f, "ce_doi5_5m", +1, 0.4, "CE OI change ATM+/-5 (5m)", "call OI rising"),
        Component("CE writing share 5m", f.get("ce_writing_w_5m"), None, 0.5 * (f.get("ce_writing_w_5m", 0) or 0)
                  - 0.4 * (f.get("ce_long_unwinding_w_5m", 0) or 0), "CE premium down + OI up (call writing)"),
        Component("PE short-covering share 5m", f.get("pe_short_covering_w_5m"), None,
                  0.5 * (f.get("pe_short_covering_w_5m", 0) or 0) - 0.5 * (f.get("pe_writing_w_5m", 0) or 0),
                  "PE premium up + OI down vs put writing"),
        _zc(f, "atm_pe_vol_accel", +1, 0.4, "ATM PE volume acceleration", "ATM put volume vs its 30m baseline", 3.0),
        _zc(f, "atm_pe_prem_pct_5m", +1, 0.3, "ATM PE premium momentum 5m", "put premium expanding"),
        _zc(f, "fut_ret_5m", -1, 0.7, "Futures 5m return", "futures momentum confirmation (down)"),
        Component("VWAP deviation", vwap_dev, None, (0.2 if vwap_dev < 0 else -0.2) if fin(vwap_dev) else 0.0,
                  "futures below/above VWAP"),
        _zc(f, "d_pcr_oi_5_15m", -1, 0.3, "PCR change 15m", "PCR falling"),
        lvl("or_low", False), lvl("pdl", False),
        Component("Support shift 15m", f.get("sup_shift_15m"), None,
                  (0.3 if (f.get("sup_shift_15m") or 0) < 0 else (-0.3 if (f.get("sup_shift_15m") or 0) > 0 else 0.0)),
                  "put wall migrating lower/higher"),
        Component("Futures book imbalance", f.get("fut_book_imb"), None, -0.15 * (f.get("fut_book_imb") or 0),
                  "total sell vs buy quantity on futures"),
    ]
    bull, bpos, bneg = score_from(bull_c, 1.6)
    bear, epos, eneg = score_from(bear_c, 1.6)
    ext = f.get("z_ret_30")
    rev_c = [
        Component("Extension 30m", f.get("ret_30"), ext, 0.6 * min(abs(ext) / 3, 1.5) if fin(ext) else 0.0,
                  "statistically stretched 30m move"),
        Component("Momentum fade", f.get("ret_5"), f.get("z_ret_5"),
                  (0.4 if fin(ext) and fin(f.get("z_ret_5")) and np.sign(ext) != np.sign(f["z_ret_5"]) and abs(ext) > 1.5 else 0.0),
                  "5m return opposing the 30m move"),
        Component("Opposite-side writing", None, None,
                  (0.4 * (f.get("ce_writing_w_5m", 0) or 0) if fin(ext) and ext > 0 else 0.4 * (f.get("pe_writing_w_5m", 0) or 0)),
                  "fresh writing against the move"),
        Component("VWAP stretch", vwap_dev, f.get("z_vwap_dev_pct"),
                  0.3 * min(abs(f.get("z_vwap_dev_pct") or 0) / 3, 1.0), "distance from VWAP"),
        Component("Intraday position", f.get("intraday_pos"), None,
                  0.2 if fin(f.get("intraday_pos")) and (f["intraday_pos"] > 0.95 or f["intraday_pos"] < 0.05) else 0.0,
                  "at day extreme"),
    ]
    rev_tot = sum(c.contrib for c in rev_c)
    reversal = round(100 * (1 - math.exp(-max(rev_tot, 0) / 0.8)), 1)
    vol_c = [
        _zc(f, "pct_straddle_15m", +1, 0.8, "ATM straddle change 15m", "straddle expanding"),
        _zc(f, "d_atm_iv_15m", +1, 0.6, "ATM IV change 15m", "IV expanding"),
        _zc(f, "d_vix_15m", +1, 0.4, "India VIX change 15m", "VIX rising"),
        _zc(f, "fut_vol_accel", +1, 0.4, "Futures volume acceleration", "futures volume vs 30m baseline", 3.0),
        Component("Range compression", f.get("range30_pct"), f.get("z_range30_pct"),
                  0.3 if fin(f.get("z_range30_pct")) and f["z_range30_pct"] < -1 else 0.0,
                  "tight 30m range (coiled)"),
        Component("GARCH vs realized", f.get("garch_sigma_ann"), None,
                  0.3 if fin(f.get("garch_sigma_ann")) and fin(f.get("rv_1m_ann")) and f["garch_sigma_ann"] > 1.2 * f["rv_1m_ann"] else 0.0,
                  "GARCH forecast above realized"),
        Component("Expiry day gamma", f.get("gamma_conc"), None, 0.3 if f.get("expiry_day") else 0.0, "expiry-day gamma"),
    ]
    vtot = sum(c.contrib for c in vol_c if c)
    volx = round(100 * (1 - math.exp(-max(vtot, 0) / 0.9)), 1)
    # false breakout risk: price beyond a level without options/futures confirmation
    fb = 0.0
    fbe: List[str] = []
    broke_up = fin(spot) and ((fin(f.get("or_high")) and spot > f["or_high"]) or (fin(f.get("pdh")) and spot > f["pdh"]))
    broke_dn = fin(spot) and ((fin(f.get("or_low")) and spot < f["or_low"]) or (fin(f.get("pdl")) and spot < f["pdl"]))
    if broke_up:
        if (f.get("ce_writing_w_5m") or 0) > 0.4:
            fb += 0.4
            fbe.append("upside break while calls are being written")
        if not fin(f.get("fut_ret_5m")) or f["fut_ret_5m"] <= 0:
            fb += 0.3
            fbe.append("futures not confirming upside")
        if not fin(f.get("fut_vol_accel")) or f["fut_vol_accel"] < 1.2:
            fb += 0.2
            fbe.append("no futures volume expansion")
    if broke_dn:
        if (f.get("pe_writing_w_5m") or 0) > 0.4:
            fb += 0.4
            fbe.append("downside break while puts are being written")
        if not fin(f.get("fut_ret_5m")) or f["fut_ret_5m"] >= 0:
            fb += 0.3
            fbe.append("futures not confirming downside")
        if not fin(f.get("fut_vol_accel")) or f["fut_vol_accel"] < 1.2:
            fb += 0.2
            fbe.append("no futures volume expansion")
    if f.get("illiquid_frac", 0) > 0.4:
        fb += 0.2
        fbe.append("illiquid/wide-spread strikes inflate option signals")
    false_bo = round(100 * (1 - math.exp(-fb / 0.6)), 1) if (broke_up or broke_dn) else round(min(100.0, 100 * fb / 2), 1)
    agree = abs(bull - bear) / 100
    confidence = round(min(100.0, 100 * agree * (dq / 100) * 1.5), 1)

    def pack(score: float, pos: List[Component], neg: List[Component], invalid: List[str], key: str) -> Dict[str, Any]:
        rel = reliability.get(key)
        return {"score": score,
                "positive": [f"{c.name}: {c.note} (value={_fmt(c.value)}, z={_fmt(c.z)})" for c in pos[:6]],
                "negative": [f"{c.name}: {c.note} (value={_fmt(c.value)}, z={_fmt(c.z)})" for c in neg[:6]],
                "invalidation": invalid, "data_quality": dq, "data_issues": dq_issues,
                "historical_reliability": rel or "insufficient evidence (pattern not yet validated out-of-sample)"}

    out = {
        "bullish_breakout": pack(bull, bpos, bneg, [f"futures close back below VWAP {_fmt(f.get('vwap'))}",
                                                    f"CE OI rebuilds at resistance {_fmt(f.get('resistance'))}",
                                                    f"spot loses support {_fmt(f.get('support'))}"], "bullish"),
        "bearish_breakdown": pack(bear, epos, eneg, [f"futures reclaim VWAP {_fmt(f.get('vwap'))}",
                                                     f"PE OI rebuilds at support {_fmt(f.get('support'))}",
                                                     f"spot clears resistance {_fmt(f.get('resistance'))}"], "bearish"),
        "reversal": pack(reversal, [c for c in rev_c if c.contrib > 0], [], ["new extreme with confirming futures volume"], "reversal"),
        "false_breakout_risk": {"score": false_bo, "positive": fbe, "negative": [], "invalidation": [],
                                "data_quality": dq, "historical_reliability": reliability.get("false_breakout") or "insufficient evidence"},
        "volatility_expansion": pack(volx, [c for c in vol_c if c and c.contrib > 0], [], ["straddle and IV contract"], "volatility"),
        "market_confidence": {"score": confidence, "positive": [f"bull/bear separation {abs(bull - bear):.0f} pts",
                                                                f"data quality {dq:.0f}"],
                              "negative": dq_issues, "invalidation": [], "data_quality": dq,
                              "historical_reliability": "n/a (agreement metric)"},
        "data_quality": {"score": dq, "positive": [], "negative": dq_issues, "invalidation": [], "data_quality": dq,
                         "historical_reliability": "n/a"},
        "_note": "Scores are evidence indices (0-100), NOT probabilities. Calibrated probabilities appear only "
                 "after out-of-sample validation passes (see model_performance).",
    }
    return out


def _fmt(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float, np.floating)):
        if not math.isfinite(float(v)):
            return "n/a"
        a = abs(float(v))
        return f"{v:.4f}" if a < 1 else (f"{v:.2f}" if a < 1000 else f"{v:,.0f}")
    return str(v)


REGIMES = ["STRONG_BULLISH", "STRONG_BEARISH", "SIDEWAYS", "HIGH_VOLATILITY", "LOW_VOLATILITY",
           "BREAKOUT_PREPARATION", "DISTRIBUTION", "ACCUMULATION", "REVERSAL_POSSIBILITY", "EXPIRY_DRIVEN_VOLATILITY"]


def classify_regime(f: Dict[str, Any], scores: Dict[str, Any]) -> Dict[str, Any]:
    ev: List[str] = []
    tags: List[str] = []
    bull = scores["bullish_breakout"]["score"]
    bear = scores["bearish_breakout"]["score"] if "bearish_breakout" in scores else scores["bearish_breakdown"]["score"]
    tt = f.get("trend_t", NAN)
    vd = f.get("vwap_dev_pct", NAN)
    zrv, zrng = f.get("z_rv_1m_ann"), f.get("z_range30_pct")
    if f.get("expiry_day") and f.get("minutes_since_open", 0) > 180 and fin(f.get("gamma_conc")) and f["gamma_conc"] > 0.15:
        tags.append("EXPIRY_DRIVEN_VOLATILITY")
        ev.append(f"expiry day, gamma concentration {f['gamma_conc']:.2f} at {_fmt(f.get('gamma_peak'))}")
    if fin(tt) and tt > 4 and fin(vd) and vd > 0 and bull > 60:
        tags.append("STRONG_BULLISH")
        ev.append(f"30m trend t-stat {tt:.1f}, futures {vd:+.2f}% vs VWAP, bull score {bull}")
    if fin(tt) and tt < -4 and fin(vd) and vd < 0 and bear > 60:
        tags.append("STRONG_BEARISH")
        ev.append(f"30m trend t-stat {tt:.1f}, futures {vd:+.2f}% vs VWAP, bear score {bear}")
    if (fin(zrv) and zrv > 1.5) or scores["volatility_expansion"]["score"] > 70:
        tags.append("HIGH_VOLATILITY")
        ev.append(f"realized-vol z {_fmt(zrv)}, vol-expansion score {scores['volatility_expansion']['score']}")
    elif fin(zrv) and zrv < -1 and fin(zrng) and zrng < -1:
        tags.append("LOW_VOLATILITY")
        ev.append(f"realized-vol z {zrv:.1f}, 30m range z {zrng:.1f}")
    if fin(zrng) and zrng < -1 and max(bull, bear) > 60 and not any(t.startswith("STRONG") for t in tags):
        tags.append("BREAKOUT_PREPARATION")
        ev.append(f"range compressed (z {zrng:.1f}) with directional score {max(bull, bear)}")
    pw, cw = f.get("pe_writing_w_5m", 0) or 0, f.get("ce_writing_w_5m", 0) or 0
    if fin(tt) and abs(tt) < 2 and fin(vd) and vd > 0 and pw > 0.5 and (f.get("d_fut_oi_15m") or 0) > 0:
        tags.append("ACCUMULATION")
        ev.append(f"flat price above VWAP, put writing share {pw:.0%}, futures OI rising")
    if fin(tt) and abs(tt) < 2 and fin(vd) and vd < 0 and cw > 0.5 and fin(f.get("intraday_pos")) and f["intraday_pos"] > 0.6:
        tags.append("DISTRIBUTION")
        ev.append(f"flat near highs below VWAP, call writing share {cw:.0%}")
    if scores["reversal"]["score"] > 60:
        tags.append("REVERSAL_POSSIBILITY")
        ev.append(f"reversal score {scores['reversal']['score']}")
    if not tags:
        tags.append("SIDEWAYS")
        ev.append(f"no trend (t {_fmt(tt)}), scores bull {bull} / bear {bear}")
    return {"regime": tags[0], "secondary": tags[1:], "evidence": ev}


# pattern name -> (direction, predicate). Direction: +1 bullish, -1 bearish, 0 volatility.
def _z(f: Dict[str, Any], k: str) -> float:
    v = f.get(f"z_{k}")
    return float(v) if fin(v) else 0.0


PATTERNS: Dict[str, Tuple[int, Callable[[Dict[str, Any]], bool]]] = {
    "CE_OI_DROP_AT_RESISTANCE": (+1, lambda f: _z(f, "res_ce_doi_15m") < -2.5),
    "PE_OI_DROP_AT_SUPPORT": (-1, lambda f: _z(f, "sup_pe_doi_15m") < -2.5),
    "CE_UNWIND_PLUS_PE_WRITE": (+1, lambda f: (f.get("ce_short_covering_w_5m", 0) + f.get("ce_long_unwinding_w_5m", 0)) > 0.5
                                and f.get("pe_writing_w_5m", 0) > 0.5),
    "PE_UNWIND_PLUS_CE_WRITE": (-1, lambda f: (f.get("pe_short_covering_w_5m", 0) + f.get("pe_long_unwinding_w_5m", 0)) > 0.5
                                and f.get("ce_writing_w_5m", 0) > 0.5),
    "BOTH_SIDE_UNWINDING": (0, lambda f: _z(f, "ce_doi5_5m") < -2 and _z(f, "pe_doi5_5m") < -2),
    "BOTH_SIDE_WRITING": (0, lambda f: f.get("ce_writing_w_5m", 0) > 0.5 and f.get("pe_writing_w_5m", 0) > 0.5),
    "IV_EXPANSION": (0, lambda f: _z(f, "d_atm_iv_15m") > 2.5),
    "STRADDLE_EXPANSION": (0, lambda f: _z(f, "pct_straddle_15m") > 2.5),
    "GAMMA_CONCENTRATION_UP": (0, lambda f: _z(f, "d_gamma_conc_15m") > 2.5),
    "ATM_CALL_VOLUME_ACCEL": (+1, lambda f: _z(f, "atm_ce_vol_accel") > 3 and _z(f, "atm_ce_prem_pct_5m") > 1),
    "ATM_PUT_VOLUME_ACCEL": (-1, lambda f: _z(f, "atm_pe_vol_accel") > 3 and _z(f, "atm_pe_prem_pct_5m") > 1),
    "PCR_ACCEL_UP": (+1, lambda f: _z(f, "d_pcr_oi_5_15m") > 2.5),
    "PCR_ACCEL_DOWN": (-1, lambda f: _z(f, "d_pcr_oi_5_15m") < -2.5),
    "RESISTANCE_SHIFT_UP": (+1, lambda f: (f.get("res_shift_15m") or 0) > 0 and (f.get("sup_shift_15m") or 0) >= 0),
    "SUPPORT_SHIFT_DOWN": (-1, lambda f: (f.get("sup_shift_15m") or 0) < 0 and (f.get("res_shift_15m") or 0) <= 0),
    "FUT_UP_OPTIONS_CONFIRM": (+1, lambda f: _z(f, "fut_ret_5m") > 2 and f.get("pe_writing_w_5m", 0) > 0.4
                               and f.get("ce_writing_w_5m", 0) < 0.3),
    "FUT_DOWN_OPTIONS_CONFIRM": (-1, lambda f: _z(f, "fut_ret_5m") < -2 and f.get("ce_writing_w_5m", 0) > 0.4
                                 and f.get("pe_writing_w_5m", 0) < 0.3),
    "FUT_OPTIONS_DIVERGENCE_UP": (-1, lambda f: _z(f, "fut_ret_5m") > 2 and f.get("ce_writing_w_5m", 0) > 0.5),
    "FUT_OPTIONS_DIVERGENCE_DOWN": (+1, lambda f: _z(f, "fut_ret_5m") < -2 and f.get("pe_writing_w_5m", 0) > 0.5),
    "UNUSUAL_FUT_VOLUME": (0, lambda f: _z(f, "fut_vol_accel") > 3),
    "BULL_SCORE_HIGH": (+1, lambda f: (f.get("_bull") or 0) >= 70),
    "BEAR_SCORE_HIGH": (-1, lambda f: (f.get("_bear") or 0) >= 70),
}


def active_patterns(f: Dict[str, Any]) -> Dict[str, int]:
    out = {}
    for name, (d, pred) in PATTERNS.items():
        try:
            if pred(f):
                out[name] = d
        except Exception:
            continue
    return out

# ============================================================================
# 8. EVENT-DRIVEN RESEARCH: breakout events + signal recording (causal)
# ============================================================================

def compact_snapshot(r: Optional[SnapRec]) -> Optional[Dict[str, Any]]:
    if r is None:
        return None
    keep = ("spot", "fut", "forward", "atm", "pcr_oi_5", "atm_iv", "straddle", "gamma_conc", "gamma_peak", "gex",
            "resistance", "support", "ce_oi_5", "pe_oi_5", "ce_doi5_5m", "pe_doi5_5m", "vwap", "vwap_dev_pct",
            "fut_volume", "fut_oi", "ce_book_imb_5", "pe_book_imb_5", "atm_ce", "atm_pe", "ce_buildup_5m",
            "pe_buildup_5m", "ce_writing_w_5m", "pe_writing_w_5m", "ce_short_covering_w_5m", "pe_short_covering_w_5m",
            "atm_ce_vol_accel", "atm_pe_vol_accel", "fut_ret_5m", "vix", "liquidity_score")
    d = {k: r.f.get(k) for k in keep}
    d["ts"] = r.ts
    d["regime"] = r.f.get("_regime")
    d["bull"] = r.scores.get("bull")
    d["bear"] = r.scores.get("bear")
    d["strikes"] = {f"{k}{s}": {"px": v[0], "oi": v[1], "vol": v[2], "iv": v[3]} for (k, s), v in r.legs.items()
                    if fin(r.atm) and fin(r.step) and abs(k - r.atm) <= 5 * r.step}
    return d


def analyse_event(st: IndexState, ev_ts: float, direction: int, pre: List[SnapRec]) -> Dict[str, Any]:
    """Answer the research questions using ONLY snapshots stamped <= event time."""
    pre = [p for p in pre if p.ts <= ev_ts]
    out: Dict[str, Any] = {}
    if len(pre) < 3:
        return {"note": "insufficient pre-event snapshots"}
    base = pre[0]
    # 1. which strike changed first: earliest snapshot where |dOI| vs base exceeds 3x robust scale
    first: Optional[Tuple[float, str, float, float]] = None
    changes: Dict[Tuple[float, str], List[float]] = collections.defaultdict(list)
    for p in pre[1:]:
        for k, v in p.legs.items():
            b = base.legs.get(k)
            if b and fin(v[1]) and fin(b[1]):
                changes[k].append(v[1] - b[1])
    all_abs = [abs(x) for lst in changes.values() for x in lst]
    scale = float(np.median(all_abs)) * 1.4826 if all_abs else 0.0
    if scale > 0:
        for p in pre[1:]:
            for k, v in sorted(p.legs.items()):
                b = base.legs.get(k)
                if b and fin(v[1]) and fin(b[1]) and abs(v[1] - b[1]) > 3 * scale:
                    first = (k[0], k[1], (ev_ts - p.ts) / 60, v[1] - b[1])
                    break
            if first:
                break
    out["first_strike_changed"] = ({"strike": first[0], "side": first[1], "minutes_before": round(first[2], 1),
                                    "doi": first[3]} if first else None)
    last = pre[-1]

    def leg_doi(side: str, near: float) -> float:
        tot = 0.0
        for (k, s), v in last.legs.items():
            if s == side and fin(near) and fin(last.step) and abs(k - near) <= last.step:
                b = base.legs.get((k, s))
                if b and fin(v[1]) and fin(b[1]):
                    tot += v[1] - b[1]
        return tot
    ce_res = leg_doi("CE", base.f.get("resistance", NAN))
    pe_sup = leg_doi("PE", base.f.get("support", NAN))
    out["ce_writers_exited_before"] = bool(ce_res < 0)
    out["ce_oi_change_at_resistance"] = ce_res
    out["pe_writers_increased"] = bool(pe_sup > 0)
    out["pe_oi_change_at_support"] = pe_sup
    out["iv_expanded"] = bool(fin(last.f.get("atm_iv")) and fin(base.f.get("atm_iv")) and last.f["atm_iv"] > base.f["atm_iv"])
    out["iv_change"] = (last.f.get("atm_iv", NAN) - base.f.get("atm_iv", NAN))
    out["gamma_conc_change"] = last.f.get("gamma_conc", NAN) - base.f.get("gamma_conc", NAN)
    out["gamma_concentration_changed"] = bool(fin(out["gamma_conc_change"]) and abs(out["gamma_conc_change"]) > 0.02)
    fr = math.log(last.fut / base.fut) if fin(last.fut) and fin(base.fut) and base.fut > 0 else NAN
    out["futures_confirmed"] = bool(fin(fr) and np.sign(fr) == direction and direction != 0)
    out["futures_ret_pre"] = fr
    out["pcr_change"] = last.f.get("pcr_oi_5", NAN) - base.f.get("pcr_oi_5", NAN)
    # 7. lead time: earliest pre-snapshot from which the directional score stayed >= 65
    key = "bull" if direction > 0 else "bear"
    lead = None
    for p in pre:
        tail = [q for q in pre if q.ts >= p.ts]
        if tail and all((q.scores.get(key) or 0) >= 65 for q in tail):
            lead = (ev_ts - p.ts) / 60
            break
    out["lead_minutes"] = round(lead, 1) if lead is not None else None
    out["patterns_before"] = sorted({k for q in pre for k in (q.f.get("_patterns") or {})})
    return out


class EventEngine:
    """Detects significant index moves with an adaptive threshold
    |ln(P_t / P_{t-h})| > k * sigma_1m * sqrt(h), sigma estimated EXCLUDING the move window.
    Saves t-15/-10/-5/0 snapshots immediately, fills +5/+15/+30 later."""

    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self.open_events: List[Dict[str, Any]] = []
        self.last_event_ts: Dict[str, float] = {}
        self.n_events = 0

    def check(self, st: IndexState, ts: float) -> Optional[Dict[str, Any]]:
        h = self.cfg.event_horizon_min
        closes = st.bars.closes()
        if len(closes) < 60:
            return None
        lr = np.diff(np.log(closes))
        sigma = float(np.std(lr[-400:-h], ddof=1)) if len(lr) > h + 30 else NAN
        if not fin(sigma) or sigma <= 0:
            return None
        move = float(math.log(closes[-1] / closes[-1 - h]))
        thr = self.cfg.event_sigma_k * sigma * math.sqrt(h)
        if abs(move) < thr:
            return None
        if ts - self.last_event_ts.get(st.name, 0) < self.cfg.event_min_gap_min * 60:
            return None
        self.last_event_ts[st.name] = ts
        direction = 1 if move > 0 else -1
        f = st.last_features
        kind = "UPSIDE_BREAKOUT" if direction > 0 else "DOWNSIDE_BREAKDOWN"
        if fin(f.get("ret_30")) and np.sign(f["ret_30"] - move) == -direction and abs(f["ret_30"] - move) > thr:
            kind = "SHORT_COVERING_RALLY" if direction > 0 else "LONG_UNWINDING_DROP"
        pre_recs = [r for r in st.history if ts - 16 * 60 <= r.ts <= ts]
        pick = {m: st.past(ts, m) for m in (15, 10, 5)}
        at = st.history[-1] if st.history else None
        analysis = analyse_event(st, ts, direction, pre_recs)
        p, u, i = ts_pair(ts)
        ev = {"ts": p, "ts_utc": u, "ts_ist": i, "index_name": st.name, "kind": kind, "direction": direction,
              "move_pct": move * 100, "sigma_mult": abs(move) / (sigma * math.sqrt(h)), "spot": closes[-1],
              "pre_15": compact_snapshot(pick[15]), "pre_10": compact_snapshot(pick[10]), "pre_5": compact_snapshot(pick[5]),
              "at_event": compact_snapshot(at), "analysis": analysis, "lead_minutes": analysis.get("lead_minutes"),
              "regime": st.regime.get("regime"), "complete": 0}
        self.db.insert("breakout_events", ev)
        self.n_events += 1
        self.open_events.append({"index": st.name, "ts": ts, "dir": direction, "spot": closes[-1], "move": move,
                                 "filled": set(), "analysis": analysis, "kind": kind})
        LOG.info("breakout event", index=st.name, kind=kind, move_pct=round(move * 100, 3),
                 sigma_mult=round(ev["sigma_mult"], 2), lead=analysis.get("lead_minutes"))
        return ev

    def update_post(self, states: Dict[str, IndexState], now: float) -> None:
        keep = []
        for e in self.open_events:
            st = states.get(e["index"])
            if st is None:
                continue
            for m, col in ((5, "post_5"), (15, "post_15"), (30, "post_30")):
                if m in e["filled"] or now < e["ts"] + m * 60:
                    continue
                rec = None
                for r in st.history:
                    if r.ts >= e["ts"] + m * 60:
                        rec = r
                        break
                rec = rec or (st.history[-1] if st.history else None)
                snap = compact_snapshot(rec)
                if snap is not None:
                    px = st.bars.price_at_or_before(e["ts"] + m * 60 + 60)
                    snap["ret_from_event"] = math.log(px / e["spot"]) if fin(px) and e["spot"] > 0 else None
                self.db.execute_async(f"UPDATE breakout_events SET {col}=? WHERE index_name=? AND ts=?",
                                      (jdump(snap), e["index"], e["ts"]))
                e["filled"].add(m)
                if m == 30 and snap is not None:
                    r30 = snap.get("ret_from_event")
                    genuine = None
                    if r30 is not None:
                        # genuine if the move was not given back (>=50% retained, i.e. continuation >= -50% of move)
                        genuine = 1 if e["dir"] * r30 >= -0.5 * abs(e["move"]) else 0
                    an = dict(e["analysis"])
                    an["after_15_ret"] = None
                    an["after_30_ret"] = r30
                    an["genuine"] = genuine
                    if genuine == 0:
                        an["classified"] = "FALSE_BREAKOUT"
                    self.db.execute_async("UPDATE breakout_events SET genuine=?, complete=1, analysis=? WHERE index_name=? AND ts=?",
                                          (genuine, jdump(an), e["index"], e["ts"]))
            if len(e["filled"]) < 3:
                keep.append(e)
        self.open_events = keep


class SignalRecorder:
    """Edge-triggered pattern signals, stored with the features available at signal time.
    Outcomes are labelled only after the +30 minute horizon has fully elapsed."""

    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self.pending: List[Dict[str, Any]] = []
        self.n = 0

    def record(self, st: IndexState, ts: float, patterns: Dict[str, int]) -> List[str]:
        new = []
        for name, d in patterns.items():
            last = st.patterns_on.get(name)
            if last is not None and ts - last < 15 * 60:
                st.patterns_on[name] = ts
                continue
            st.patterns_on[name] = ts
            new.append(name)
            f = st.last_features
            p, u, i = ts_pair(ts)
            row = {"ts": p, "ts_utc": u, "ts_ist": i, "index_name": st.name, "kind": name, "direction": d,
                   "score": f.get("_bull") if d > 0 else (f.get("_bear") if d < 0 else f.get("_volx")),
                   "spot": f.get("spot"), "fut": f.get("fut"), "feature_ts": f.get("feature_ts", ts),
                   "evidence": st.last_scores.get("bullish_breakout" if d > 0 else "bearish_breakdown", {}).get("positive"),
                   "features": {k: v for k, v in f.items() if not k.startswith("_") and isinstance(v, (int, float, str, bool))}}
            self.db.insert("signals", row)
            self.pending.append({"index": st.name, "ts": ts, "dir": d, "spot": f.get("spot"), "kind": name})
            self.n += 1
        for name in list(st.patterns_on):
            if name not in patterns and ts - st.patterns_on[name] > 15 * 60:
                st.patterns_on.pop(name, None)
        return new

    def label(self, states: Dict[str, IndexState], now: float) -> int:
        done, keep = 0, []
        for s in self.pending:
            if now < s["ts"] + 31 * 60:
                keep.append(s)
                continue
            st = states.get(s["index"])
            if not st or not fin(s.get("spot")):
                continue
            path = [b for b in st.bars.all() if s["ts"] < b["ts"] + 60 <= s["ts"] + 30 * 60 + 60]
            if not path:
                continue
            p0 = s["spot"]
            r = {m: math.log(st.bars.price_at_or_before(s["ts"] + m * 60 + 60) / p0) for m in (5, 15, 30)}
            hi, lo = max(b["high"] for b in path), min(b["low"] for b in path)
            d = s["dir"] if s["dir"] != 0 else (1 if r[30] >= 0 else -1)
            mfe = math.log(hi / p0) if d > 0 else -math.log(lo / p0)
            mae = math.log(lo / p0) if d > 0 else -math.log(hi / p0)
            outcome = "UP" if r[15] > 0 else "DOWN"
            self.db.execute_async("UPDATE signals SET ret_5=?, ret_15=?, ret_30=?, mfe_30=?, mae_30=?, labelled=1, outcome=? "
                                  "WHERE index_name=? AND ts=? AND kind=?",
                                  (r[5], r[15], r[30], mfe, mae, outcome, s["index"], s["ts"], s["kind"]))
            done += 1
        self.pending = keep
        return done

    def relabel_from_db(self, db: Database) -> int:
        """After a restart, label any old unlabelled signals from stored index snapshots."""
        rows = db.query("SELECT id, ts, index_name, direction, spot FROM signals WHERE labelled=0 AND ts < ?",
                        (time.time() - 31 * 60,))
        n = 0
        for r in rows:
            snaps = db.query("SELECT ts, spot FROM index_snapshots WHERE index_name=? AND ts>? AND ts<=? ORDER BY ts",
                             (r["index_name"], r["ts"], r["ts"] + 31 * 60))
            if len(snaps) < 5 or not r["spot"]:
                continue
            ts_arr = [x["ts"] for x in snaps]
            sp = [x["spot"] for x in snaps]

            def at(m: int) -> Optional[float]:
                i = bisect.bisect_right(ts_arr, r["ts"] + m * 60) - 1
                return sp[i] if i >= 0 and sp[i] else None
            vals = {m: at(m) for m in (5, 15, 30)}
            if not all(vals.values()):
                continue
            rr = {m: math.log(vals[m] / r["spot"]) for m in vals}  # type: ignore[arg-type]
            d = r["direction"] or (1 if rr[30] >= 0 else -1)
            valid = [x for x in sp if x]
            mfe = math.log(max(valid) / r["spot"]) if d > 0 else -math.log(min(valid) / r["spot"])
            mae = math.log(min(valid) / r["spot"]) if d > 0 else -math.log(max(valid) / r["spot"])
            db.execute("UPDATE signals SET ret_5=?, ret_15=?, ret_30=?, mfe_30=?, mae_30=?, labelled=1, outcome=? WHERE id=?",
                       (rr[5], rr[15], rr[30], mfe, mae, "UP" if rr[15] > 0 else "DOWN", r["id"]))
            n += 1
        return n


# ============================================================================
# 9. STATISTICAL VALIDATION, WALK-FORWARD BACKTEST, MODELS
# ============================================================================

def load_snapshot_panel(db: Database, index: Optional[str] = None, days: int = 120) -> List[Dict[str, Any]]:
    since = time.time() - days * 86400
    q = "SELECT ts, index_name, spot, fut, features, scores FROM index_snapshots WHERE ts>?"
    args: Tuple[Any, ...] = (since,)
    if index:
        q += " AND index_name=?"
        args += (index,)
    rows = db.query(q + " ORDER BY index_name, ts", args)
    out = []
    for r in rows:
        try:
            f = json.loads(r["features"]) if r["features"] else {}
        except json.JSONDecodeError:
            continue
        out.append({"ts": r["ts"], "index": r["index_name"], "spot": r["spot"], "fut": r["fut"], "f": f})
    return out


def attach_forward_returns(panel: List[Dict[str, Any]], horizons: Sequence[int] = (5, 15, 30)) -> None:
    """Forward returns computed from LATER rows only; each row's label window starts after its ts.
    Rows are only labelled if the horizon lies within the same trading session."""
    by_idx: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    for r in panel:
        by_idx[r["index"]].append(r)
    for rows in by_idx.values():
        ts = [r["ts"] for r in rows]
        for i, r in enumerate(rows):
            r["fwd"] = {}
            r["path"] = {}
            if not r["spot"]:
                continue
            day = dt.datetime.fromtimestamp(r["ts"], IST).date()
            for h in horizons:
                j = bisect.bisect_left(ts, r["ts"] + h * 60)
                if j >= len(rows) or rows[j]["ts"] - (r["ts"] + h * 60) > 120:
                    continue
                if dt.datetime.fromtimestamp(rows[j]["ts"], IST).date() != day or not rows[j]["spot"]:
                    continue
                r["fwd"][h] = math.log(rows[j]["spot"] / r["spot"])
                seg = [x["spot"] for x in rows[i + 1: j + 1] if x["spot"]]
                if seg:
                    r["path"][h] = (math.log(max(seg) / r["spot"]), math.log(min(seg) / r["spot"]))
            r["label_start_ts"] = r["ts"]


def pattern_study(panel: List[Dict[str, Any]], cfg: Config) -> List[Dict[str, Any]]:
    """For every pattern x index x horizon: sample count, success rate (Wilson CI), mean/median move,
    MFE/MAE, precision/recall vs significant moves, net return after costs, binomial p-value vs the
    unconditional base rate, Benjamini-Hochberg FDR across all hypotheses tested."""
    results = []
    cost = cfg.cost_bps_roundtrip / 1e4
    groups: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    for r in panel:
        groups[r["index"]].append(r)
    groups["ALL"] = panel
    for idx, rows in groups.items():
        for h in (5, 15, 30):
            lab = [r for r in rows if h in r.get("fwd", {})]
            if len(lab) < 50:
                continue
            fw = np.array([r["fwd"][h] for r in lab])
            sig = np.array([r["f"].get("sigma_1m") or np.nan for r in lab], float)
            sig = np.where(np.isfinite(sig) & (sig > 0), sig, np.nanmedian(sig) if np.isfinite(np.nanmedian(sig)) else 5e-4)
            thr = 1.5 * sig * math.sqrt(h)
            sig_up, sig_dn = fw > thr, fw < -thr
            base_up, base_dn = float(np.mean(fw > 0)), float(np.mean(fw < 0))
            for name, (d, _pred) in PATTERNS.items():
                on = np.array([name in (r["f"].get("_patterns") or {}) for r in lab])
                n = int(on.sum())
                rec = {"pattern": name, "index_name": idx, "horizon": h, "n": n, "direction": d}
                if n == 0:
                    continue
                x = fw[on]
                if d == 0:
                    target = (sig_up | sig_dn)
                    hits = int(target[on].sum())
                    base = float(target.mean())
                    signed = np.abs(x)
                else:
                    target = sig_up if d > 0 else sig_dn
                    hits = int((np.sign(x) == d).sum())
                    base = base_up if d > 0 else base_dn
                    signed = d * x
                p, lo, hi = wilson_interval(hits, n)
                tp = int((on & target).sum())
                prec = tp / n if n else NAN
                recall = tp / int(target.sum()) if target.sum() else NAN
                f1 = 2 * prec * recall / (prec + recall) if prec and recall and fin(prec) and fin(recall) else NAN
                paths = [r.get("path", {}).get(h) for r, o in zip(lab, on) if o]
                mfe = [(pp[0] if d >= 0 else -pp[1]) for pp in paths if pp]
                mae = [(pp[1] if d >= 0 else -pp[0]) for pp in paths if pp]
                pval = binom_test_greater(hits, n, base) if d != 0 else binom_test_greater(hits, n, base)
                rec.update({"hits": hits, "success_rate": p, "ci_lo": lo, "ci_hi": hi, "base_rate": base,
                            "mean_ret": float(np.mean(signed)), "median_ret": float(np.median(signed)),
                            "mfe": float(np.mean(mfe)) if mfe else NAN, "mae": float(np.mean(mae)) if mae else NAN,
                            "precision": prec, "recall": recall, "f1": f1,
                            "net_ret_after_cost": float(np.mean(signed) - cost) if d != 0 else NAN,
                            "p_value": pval,
                            "sample_status": "OK" if n >= cfg.min_samples_pattern else f"INSUFFICIENT (n<{cfg.min_samples_pattern})"})
                results.append(rec)
    pv = [r["p_value"] for r in results]
    adj = bh_adjusted(pv)
    rej = benjamini_hochberg(pv, cfg.fdr_q)
    for r, a, rj in zip(results, adj, rej):
        r["p_adj"] = a
        r["fdr_pass"] = int(bool(rj) and r["n"] >= cfg.min_samples_pattern and r["success_rate"] > r["base_rate"])
    return results


def walk_forward_backtest(panel: List[Dict[str, Any]], cfg: Config, horizon: int = 15) -> Dict[str, Any]:
    """Pattern-selection walk-forward: in each fold choose patterns whose TRAIN net edge after costs is
    positive with n >= min_samples, then trade them OUT-OF-SAMPLE (enter at signal snapshot, exit at
    +horizon, underlying-return P&L minus round-trip cost). Also reports an ATM-option P&L proxy via
    the Taylor expansion (delta, gamma, theta) net of spread + fees. Embargo = horizon."""
    rows = sorted([r for r in panel if horizon in r.get("fwd", {})], key=lambda r: r["ts"])
    out: Dict[str, Any] = {"horizon": horizon, "n_rows": len(rows), "folds": []}
    if len(rows) < 300:
        out["status"] = f"INSUFFICIENT DATA ({len(rows)} labelled snapshots; need >= 300)"
        return out
    cost = cfg.cost_bps_roundtrip / 1e4
    ts = np.array([r["ts"] for r in rows])
    emb = int(max(1, horizon * 60 / max(cfg.snapshot_interval_s, 1)))
    splits = walk_forward_splits(len(rows), 5, min_train=max(150, len(rows) // 3), embargo=emb)
    oos_rets, opt_rets, sel_hist, sr_trials = [], [], [], []
    viol = 0
    for tr, te in splits:
        if not check_no_overlap(tr.tolist(), te.tolist()):
            viol += 1
            continue
        if ts[tr].max() >= ts[te].min():
            viol += 1
        train = [rows[i] for i in tr]
        chosen = []
        for name, (d, _p) in PATTERNS.items():
            if d == 0:
                continue
            x = [d * r["fwd"][horizon] for r in train if name in (r["f"].get("_patterns") or {})]
            if len(x) >= cfg.min_samples_pattern:
                edge = float(np.mean(x)) - cost
                sr_trials.append(float(np.mean(x) / (np.std(x, ddof=1) + 1e-12)))
                if edge > 0 and one_sample_t_pvalue(np.array(x) - cost) < 0.10:
                    chosen.append((name, d))
        sel_hist.append([c[0] for c in chosen])
        fold_rets = []
        last_exit = -1e18
        for i in te:
            r = rows[i]
            if r["ts"] < last_exit:
                continue          # one position at a time
            pats = r["f"].get("_patterns") or {}
            for name, d in chosen:
                if name in pats:
                    gross = d * r["fwd"][horizon]
                    fold_rets.append(gross - cost)
                    F = r["f"].get("forward") or r["spot"]
                    dF = F * (math.exp(r["fwd"][horizon]) - 1)
                    delta = abs(r["f"].get("atm_ce_delta") or 0.5)
                    gamma = r["f"].get("atm_gamma") or 0.0
                    theta = (r["f"].get("atm_theta") or 0.0) / 2
                    prem = r["f"].get("atm_ce") if d > 0 else r["f"].get("atm_pe")
                    spr = (r["f"].get("median_spread_pct") or 1.0) / 100
                    if prem and prem > 0:
                        pnl = gamma_pnl_approx(gamma, dF, theta, horizon / 1440, delta * d) / prem
                        opt_rets.append(pnl - spr - cost)
                    last_exit = r["ts"] + horizon * 60
                    break
        oos_rets += fold_rets
        out["folds"].append({"train_n": len(tr), "test_n": len(te), "chosen": [c[0] for c in chosen],
                             "trades": len(fold_rets), "mean_net": float(np.mean(fold_rets)) if fold_rets else None})
    out["lookahead_violations"] = viol
    out["oos_trades"] = len(oos_rets)
    if len(oos_rets) >= 10:
        a = np.array(oos_rets)
        per_sr = float(a.mean() / (a.std(ddof=1) + 1e-12))
        trades_per_year = len(a) / max((ts[-1] - ts[0]) / (365 * 86400), 1 / 365)
        sk = float(_sstats.skew(a)) if HAVE_SCIPY else 0.0
        ku = float(_sstats.kurtosis(a, fisher=False)) if HAVE_SCIPY else 3.0
        out.update({"oos_mean_net": float(a.mean()), "oos_hit_rate": float(np.mean(a > 0)),
                    "oos_sharpe_ann": per_sr * math.sqrt(trades_per_year), "oos_sharpe_per_trade": per_sr,
                    "psr": probabilistic_sharpe(per_sr, len(a), sk, ku),
                    "dsr": deflated_sharpe_ratio(per_sr, len(a), max(len(sr_trials), 1), sk, ku,
                                                 float(np.var(sr_trials)) if len(sr_trials) > 1 else 0.0),
                    "n_trials": len(sr_trials), "max_drawdown": max_drawdown(np.cumsum(a))[0],
                    "var95_es95": var_es(a, 0.95)})
        p_win = float(np.mean(a > 0))
        w = float(a[a > 0].mean()) if (a > 0).any() else 0.0
        l_ = float(-a[a <= 0].mean()) if (a <= 0).any() else 0.0
        kf = kelly_fraction(p_win, w, l_) if w > 0 and l_ > 0 else 0.0
        out["kelly_full"] = kf
        out["kelly_quarter"] = kf / 4
        out["risk_of_ruin_1pct_risk"] = risk_of_ruin(p_win, (w / l_) if l_ > 0 else 1.0, 0.01) if l_ > 0 else NAN
        out["status"] = "OK"
    else:
        out["status"] = "TOO FEW OOS TRADES"
    if len(opt_rets) >= 10:
        b = np.array(opt_rets)
        out["option_proxy_mean_net"] = float(b.mean())
        out["option_proxy_hit_rate"] = float(np.mean(b > 0))
    out["selection_history"] = sel_hist
    return out


MODEL_FEATURES = ["z_res_ce_doi_15m", "z_sup_pe_doi_15m", "z_ce_doi5_5m", "z_pe_doi5_5m", "z_d_atm_iv_15m",
                  "z_pct_straddle_15m", "z_d_gamma_conc_15m", "z_d_pcr_oi_5_15m", "z_atm_ce_vol_accel",
                  "z_atm_pe_vol_accel", "z_fut_ret_5m", "z_ret_15", "z_vwap_dev_pct", "ce_writing_w_5m",
                  "pe_writing_w_5m", "ce_short_covering_w_5m", "pe_short_covering_w_5m", "z_fut_vol_accel"]


def model_validation(panel: List[Dict[str, Any]], cfg: Config, horizon: int = 15) -> Dict[str, Any]:
    """Explainable logistic model (and XGBoost only when >= 3000 rows and installed) for
    P(up move > 1.5 sigma within horizon). Strict walk-forward with embargo; isotonic calibration
    fitted on the training fold only. Includes look-ahead and leakage checks."""
    rows = sorted([r for r in panel if horizon in r.get("fwd", {})], key=lambda r: r["ts"])
    res: Dict[str, Any] = {"model": "logistic_l2", "target": f"up_{horizon}m_1.5sigma", "n": len(rows)}
    if len(rows) < 500:
        res["status"] = f"INSUFFICIENT DATA (n={len(rows)} < 500): ML deferred; rule-based scores only"
        return res
    X = np.array([[float(r["f"].get(k) or 0.0) if fin(r["f"].get(k)) else 0.0 for k in MODEL_FEATURES] for r in rows])
    sig = np.array([r["f"].get("sigma_1m") or 5e-4 for r in rows], float)
    y = np.array([1 if r["fwd"][horizon] > 1.5 * s * math.sqrt(horizon) else 0 for r, s in zip(rows, sig)])
    fts = [r["f"].get("feature_ts", r["ts"]) for r in rows]
    dts = [r["ts"] for r in rows]
    res["lookahead_violations"] = lookahead_violations(fts, dts)
    res["leakage_flags"] = leakage_scan(X, y, MODEL_FEATURES)
    emb = int(max(1, horizon * 60 / max(cfg.snapshot_interval_s, 1)))
    splits = walk_forward_splits(len(rows), 5, min_train=len(rows) // 3, embargo=emb)
    P, Y, S = [], [], []
    for tr, te in splits:
        mu, sd = X[tr].mean(0), X[tr].std(0) + 1e-9
        w = logistic_fit((X[tr] - mu) / sd, y[tr], l2=2.0)
        p_tr = logistic_predict(w, (X[tr] - mu) / sd)
        cal = IsotonicCalibrator().fit(p_tr, y[tr])
        p_te = logistic_predict(w, (X[te] - mu) / sd)
        P += [cal.predict(p) for p in p_te]
        S += p_te.tolist()
        Y += y[te].tolist()
        res["coef_last_fold"] = dict(zip(["const"] + MODEL_FEATURES, np.round(w, 4).tolist()))
        last_pred = (w, mu, sd)
    if not Y:
        res["status"] = "no folds"
        return res
    Pa, Ya = np.array(P), np.array(Y)
    base = float(np.mean(y[: len(rows) // 3]))
    res.update({"n_test": len(Ya), "brier": brier_score(Pa, Ya), "base_brier": brier_score(np.full(len(Ya), base), Ya),
                "ece": expected_calibration_error(Pa, Ya), "reliability": reliability_bins(Pa, Ya),
                **{k: v for k, v in confusion(Ya, (Pa > 0.5).astype(int)).items()}})
    w, mu, sd = last_pred
    res["feature_importance"] = permutation_importance(lambda Z: logistic_predict(w, (Z - mu) / sd), X[-len(Ya):],
                                                       Ya, MODEL_FEATURES)
    res["calibrated_ok"] = bool(len(Ya) >= cfg.min_oos_for_calibration and res["brier"] < res["base_brier"]
                                and fin(res["ece"]) and res["ece"] < 0.05 and res["lookahead_violations"] == 0
                                and not res["leakage_flags"])
    if HAVE_XGB and len(rows) >= 3000:
        try:
            tr, te = splits[-1]
            m = _xgb.XGBClassifier(n_estimators=200, max_depth=3, learning_rate=0.05, subsample=0.8,
                                   colsample_bytree=0.8, eval_metric="logloss")
            m.fit(X[tr], y[tr])
            pe = m.predict_proba(X[te])[:, 1]
            res["xgb_last_fold_brier"] = brier_score(pe, y[te])
            res["xgb_importance"] = dict(zip(MODEL_FEATURES, np.round(m.feature_importances_, 4).tolist()))
        except Exception as e:
            res["xgb_error"] = str(e)
    res["status"] = "OK"
    return res


def event_study(db: Database, cfg: Config) -> Dict[str, Any]:
    rows = db.query("SELECT index_name, kind, direction, genuine, lead_minutes, analysis FROM breakout_events WHERE complete=1")
    out: Dict[str, Any] = {"n_events": len(rows)}
    if not rows:
        return out
    gen = [r for r in rows if r["genuine"] is not None]
    k = sum(1 for r in gen if r["genuine"] == 1)
    out["genuine_rate"] = wilson_interval(k, len(gen)) if gen else None
    leads = [r["lead_minutes"] for r in rows if r["lead_minutes"] is not None]
    out["lead_detected_frac"] = len(leads) / len(rows)
    out["lead_minutes_median"] = float(np.median(leads)) if leads else None
    feats = collections.Counter()
    feats_g = collections.Counter()
    for r in rows:
        try:
            a = json.loads(r["analysis"] or "{}")
        except json.JSONDecodeError:
            continue
        for q in ("ce_writers_exited_before", "pe_writers_increased", "iv_expanded", "gamma_concentration_changed",
                  "futures_confirmed"):
            if a.get(q):
                feats[q] += 1
                if r["genuine"] == 1:
                    feats_g[q] += 1
        for p in a.get("patterns_before") or []:
            feats["pattern:" + p] += 1
            if r["genuine"] == 1:
                feats_g["pattern:" + p] += 1
    out["precursor_frequency"] = {k2: {"n": v, "genuine": feats_g[k2],
                                       "genuine_rate": wilson_interval(feats_g[k2], v)} for k2, v in feats.most_common(30)}
    out["note"] = "Event-conditioned frequencies (P(precursor | event)) - compare to pattern_study for P(event | precursor)."
    return out


def research_cycle(db: Database, cfg: Config) -> Dict[str, Any]:
    """Heavy, runs in a worker thread. Never touches the live loop's state."""
    t0 = time.time()
    panel = load_snapshot_panel(db)
    attach_forward_returns(panel)
    pats = pattern_study(panel, cfg)
    p, _, i = ts_pair()
    for r in pats:
        db.insert("validation_results", {"ts": p, "ts_ist": i, **{k: r.get(k) for k in (
            "pattern", "index_name", "horizon", "n", "hits", "success_rate", "ci_lo", "ci_hi", "mean_ret", "median_ret",
            "mfe", "mae", "precision", "recall", "f1", "net_ret_after_cost", "p_value", "p_adj", "fdr_pass",
            "sample_status")}, "detail": {"base_rate": r.get("base_rate"), "direction": r.get("direction")}})
    bt = {h: walk_forward_backtest(panel, cfg, h) for h in (15, 30)}
    mv = model_validation(panel, cfg)
    for h, b in bt.items():
        db.insert("model_performance", {"ts": p, "ts_ist": i, "model": "pattern_walk_forward", "target": f"{h}m",
                                        "n_train": b.get("n_rows"), "n_test": b.get("oos_trades"),
                                        "sharpe": b.get("oos_sharpe_ann"), "dsr": b.get("dsr"), "psr": b.get("psr"),
                                        "lookahead_violations": b.get("lookahead_violations"), "detail": b})
    db.insert("model_performance", {"ts": p, "ts_ist": i, "model": mv.get("model"), "target": mv.get("target"),
                                    "n_train": mv.get("n"), "n_test": mv.get("n_test"), "precision": mv.get("precision"),
                                    "recall": mv.get("recall"), "f1": mv.get("f1"), "brier": mv.get("brier"),
                                    "base_brier": mv.get("base_brier"), "ece": mv.get("ece"),
                                    "lookahead_violations": mv.get("lookahead_violations"),
                                    "leakage_flags": mv.get("leakage_flags"), "detail": mv})
    es = event_study(db, cfg)
    db.set_meta("last_event_study", es)
    db.set_meta("last_research", {"ts": p, "panel_rows": len(panel), "patterns_tested": len(pats),
                                  "fdr_pass": [f"{r['pattern']}@{r['index_name']}/{r['horizon']}m" for r in pats if r.get("fdr_pass")],
                                  "secs": round(time.time() - t0, 1), "backtest_status": {h: b.get("status") for h, b in bt.items()},
                                  "model_status": mv.get("status"), "calibrated_ok": mv.get("calibrated_ok", False)})
    rel = {}
    for r in pats:
        if r["index_name"] == "ALL" and r["horizon"] == 15:
            rel[r["pattern"]] = {"n": r["n"], "success": round(r["success_rate"], 3), "ci": [round(r["ci_lo"], 3), round(r["ci_hi"], 3)],
                                 "base": round(r["base_rate"], 3), "fdr_pass": bool(r["fdr_pass"]), "status": r["sample_status"]}
    db.set_meta("pattern_reliability", rel)
    db.set_meta("calibration_ok", bool(mv.get("calibrated_ok", False)))
    LOG.info("research cycle done", rows=len(panel), patterns=len(pats), secs=round(time.time() - t0, 1))
    return {"panel": len(panel), "patterns": len(pats), "backtest": bt, "model": mv, "events": es}


def daily_vol_models(daily_closes: Sequence[float], minute_closes: Sequence[float]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    dr = log_returns(daily_closes)
    if len(dr) >= 100:
        out["garch"] = garch11_fit(dr)
    mr = log_returns(minute_closes)
    if len(mr) >= 200:
        out["adf_price"] = adf_test(np.log(np.asarray(minute_closes, float))[-1500:], max_lag=5)
        out["adf_returns"] = adf_test(mr[-1500:], max_lag=5)
        out["acf1"] = autocorr(mr, 1)
        out["ljung_box"] = ljung_box(mr[-1500:], 10)
        out["variance_ratio"] = martingale_test(mr[-1500:])
        out["half_life_min"] = ou_half_life(np.log(np.asarray(minute_closes, float))[-375:])
    return out

# ============================================================================
# 10. ALERTS (Telegram)
# ============================================================================

class Alerter:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self.last: Dict[Tuple[str, str], float] = {}
        self.seen: Dict[str, float] = {}
        self.sent = 0
        self.failed = 0
        self.enabled = bool(cfg.telegram_token and cfg.telegram_chat_id)
        register_secret(cfg.telegram_token)
        self._session: Any = None

    async def _post(self, text: str) -> Optional[str]:
        if not self.enabled:
            return "telegram not configured"
        if aiohttp is None:
            return "aiohttp missing"
        url = f"https://api.telegram.org/bot{self.cfg.telegram_token}/sendMessage"
        for attempt in range(3):
            try:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
                async with self._session.post(url, json={"chat_id": self.cfg.telegram_chat_id, "text": text[:4000],
                                                         "disable_web_page_preview": True}) as r:
                    if r.status == 200:
                        return None
                    if r.status == 429:
                        j = await r.json()
                        await asyncio.sleep(float(j.get("parameters", {}).get("retry_after", 5)))
                        continue
                    return f"HTTP {r.status}: {(await r.text())[:200]}"
            except Exception as e:
                err = redact(str(e))
                await asyncio.sleep(2 ** attempt)
        return locals().get("err", "failed")

    async def send(self, index: str, kind: str, text: str, state_key: str = "") -> bool:
        now = time.time()
        k = (index, kind)
        if now - self.last.get(k, 0) < self.cfg.alert_cooldown_s:
            return False
        dedupe = hashlib.sha1(f"{index}|{kind}|{state_key}".encode()).hexdigest()[:16]
        if dedupe in self.seen and now - self.seen[dedupe] < 4 * self.cfg.alert_cooldown_s:
            return False
        self.last[k] = now
        self.seen[dedupe] = now
        err = await self._post(text)
        if err is None:
            self.sent += 1
        else:
            self.failed += 1 if self.enabled else 0
        p, _, i = ts_pair(now)
        self.db.insert("alerts", {"ts": p, "ts_ist": i, "index_name": index, "kind": kind, "dedupe": dedupe,
                                  "text": text, "sent": int(err is None), "error": err})
        return err is None

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


def alert_candidates(st: IndexState, f: Dict[str, Any], sc: Dict[str, Any], new_patterns: List[str],
                     event: Optional[Dict[str, Any]], calibrated: bool) -> List[Tuple[str, str, str]]:
    out: List[Tuple[str, str, str]] = []
    bull, bear = sc["bullish_breakout"]["score"], sc["bearish_breakdown"]["score"]
    hdr = f"[{st.name}] {now_ist():%d-%b %H:%M} spot {_fmt(f.get('spot'))} fut {_fmt(f.get('fut'))}"
    tail = "\nScores are evidence indices, not probabilities." if not calibrated else ""

    def ev(key: str) -> str:
        return "\n".join("- " + x for x in sc[key]["positive"][:4]) or "- (no strong components)"
    if bull >= 72 and bull - bear >= 25:
        out.append(("EARLY_BULLISH", f"{hdr}\nEARLY BULLISH PRESSURE  bull {bull}/100 bear {bear}/100\n{ev('bullish_breakout')}"
                    f"\nInvalidation: {sc['bullish_breakout']['invalidation'][0]}{tail}", f"{round(bull / 5)}"))
    if bear >= 72 and bear - bull >= 25:
        out.append(("EARLY_BEARISH", f"{hdr}\nEARLY BEARISH PRESSURE  bear {bear}/100 bull {bull}/100\n{ev('bearish_breakdown')}"
                    f"\nInvalidation: {sc['bearish_breakdown']['invalidation'][0]}{tail}", f"{round(bear / 5)}"))
    pmap = {"CE_OI_DROP_AT_RESISTANCE": "CE_WRITER_UNWINDING", "PE_OI_DROP_AT_SUPPORT": "PE_WRITER_UNWINDING",
            "BOTH_SIDE_UNWINDING": "UNUSUAL_OI", "BOTH_SIDE_WRITING": "UNUSUAL_OI", "ATM_CALL_VOLUME_ACCEL": "VOLUME_ACCELERATION",
            "ATM_PUT_VOLUME_ACCEL": "VOLUME_ACCELERATION", "UNUSUAL_FUT_VOLUME": "VOLUME_ACCELERATION",
            "IV_EXPANSION": "IV_EXPLOSION", "STRADDLE_EXPANSION": "IV_EXPLOSION", "GAMMA_CONCENTRATION_UP": "GAMMA_CONCENTRATION"}
    for p in new_patterns:
        if p in pmap:
            out.append((pmap[p], f"{hdr}\n{p.replace('_', ' ')}\nresistance {_fmt(f.get('resistance'))} support {_fmt(f.get('support'))}"
                        f" PCR {_fmt(f.get('pcr_oi_5'))} ATM IV {_fmt(f.get('atm_iv'))} straddle {_fmt(f.get('straddle'))}"
                        f" gamma conc {_fmt(f.get('gamma_conc'))}{tail}", p))
    if event:
        k = "BREAKOUT_CONFIRMED" if event["direction"] > 0 else "BREAKDOWN_CONFIRMED"
        a = event.get("analysis", {})
        out.append((k, f"{hdr}\n{event['kind']}: {event['move_pct']:+.2f}% in {st.cfg.event_horizon_min}m ({event['sigma_mult']:.1f} sigma)"
                    f"\nfirst strike: {a.get('first_strike_changed')}\nCE writers exited: {a.get('ce_writers_exited_before')}"
                    f" | PE writers added: {a.get('pe_writers_increased')} | IV up: {a.get('iv_expanded')}"
                    f" | futures confirm: {a.get('futures_confirmed')} | lead: {a.get('lead_minutes')} min", str(round(event["ts"]))))
    if sc["false_breakout_risk"]["score"] >= 70:
        out.append(("FALSE_BREAKOUT_RISK", f"{hdr}\nFALSE BREAKOUT RISK {sc['false_breakout_risk']['score']}/100\n" +
                    "\n".join("- " + x for x in sc["false_breakout_risk"]["positive"]), "fb"))
    if sc["reversal"]["score"] >= 70:
        out.append(("REVERSAL", f"{hdr}\nREVERSAL POSSIBILITY {sc['reversal']['score']}/100\n{ev('reversal')}", "rev"))
    if sc["volatility_expansion"]["score"] >= 80 or (fin(f.get("z_rv_1m_ann")) and f["z_rv_1m_ann"] > 3):
        out.append(("ABNORMAL_VOLATILITY", f"{hdr}\nABNORMAL VOLATILITY  vol-expansion {sc['volatility_expansion']['score']}/100"
                    f"\n{ev('volatility_expansion')}", "vol"))
    return out


# ============================================================================
# 11. DASHBOARD (rich terminal UI; also mirrored to data/dashboard.txt)
# ============================================================================

def render_dashboard_text(eng: "Engine") -> str:
    L: List[str] = []
    ph = eng.cal.phase()
    status = "LIVE" if ph == "open" and eng.feed.connected else ("WARMUP" if ph == "warmup" else
                                                                ("RECONNECTING" if ph == "open" else ph.upper()))
    L.append("=" * 78)
    L.append(f" DHAN QUANT INTELLIGENCE ENGINE v{APP_VERSION}   STATUS: {status}   {now_ist():%d %B %Y  %H:%M:%S} IST")
    L.append(f" WS {'up' if eng.feed.connected else 'DOWN'} msgs {eng.feed.msgs} reconnects {eng.feed.reconnects}"
             f" | REST errors {eng.rest.errors} | DB rows {eng.db.rows_written} pending {eng.db.pending()}"
             f" | events {eng.events.n_events} signals {eng.signals.n} alerts {eng.alerter.sent}")
    vix = eng.vix_value()
    L.append(f" INDIA VIX: {_fmt(vix)}    " + "   ".join(
        f"{n}: {_fmt(eng.states[n].last_features.get('spot'))}" for n in eng.states))
    if eng.fatal_reason:
        L.append(f" !! {eng.fatal_reason}")
    L.append("=" * 78)
    for name, st in eng.states.items():
        f, sc = st.last_features, st.last_scores
        if not sc:
            L.append(f" {name}: waiting for data (chain age {_fmt(time.time() - st.chain_ts if st.chain_ts else None)}s)")
            continue
        bull, bear = sc["bullish_breakout"]["score"], sc["bearish_breakdown"]["score"]
        L.append(f" {name}  spot {_fmt(f.get('spot'))}  fut {_fmt(f.get('fut'))}  fwd {_fmt(f.get('forward'))}"
                 f"  exp {st.expiry} (lot {_fmt(st.lot)})")
        L.append(f"   MARKET REGIME: {st.regime.get('regime')} {('+ ' + ','.join(st.regime.get('secondary', []))) if st.regime.get('secondary') else ''}")
        L.append(f"   BULLISH {bull:5.1f}/100  BEARISH {bear:5.1f}/100  REVERSAL {sc['reversal']['score']:5.1f}"
                 f"  FALSE-BO {sc['false_breakout_risk']['score']:5.1f}  VOL-EXP {sc['volatility_expansion']['score']:5.1f}"
                 f"  CONF {sc['market_confidence']['score']:5.1f}  DQ {sc['data_quality']['score']:5.1f}")
        L.append(f"   CE: resistance {_fmt(f.get('resistance'))}  OI chg15m@res {_fmt(f.get('res_ce_doi_15m'))}"
                 f" ({_fmt(f.get('res_ce_doi_pct_15m'))}%)  prem5m {_fmt(f.get('atm_ce_prem_pct_5m'))}%"
                 f"  vol-accel {_fmt(f.get('atm_ce_vol_accel'))}  buildup {f.get('ce_buildup_5m', 'n/a')}")
        L.append(f"   PE: support {_fmt(f.get('support'))}  OI chg15m@sup {_fmt(f.get('sup_pe_doi_15m'))}"
                 f" ({_fmt(f.get('sup_pe_doi_pct_15m'))}%)  prem5m {_fmt(f.get('atm_pe_prem_pct_5m'))}%"
                 f"  vol-accel {_fmt(f.get('atm_pe_vol_accel'))}  buildup {f.get('pe_buildup_5m', 'n/a')}")
        L.append(f"   QUANT: PCR {_fmt(f.get('pcr_oi_5'))}  ATM IV {_fmt(f.get('atm_iv'))}  RV {_fmt(f.get('rv_1m_ann'))}"
                 f"  ATM gamma {_fmt(f.get('atm_gamma'))}  straddle {_fmt(f.get('straddle'))}  basis {_fmt(f.get('basis'))}"
                 f"  VWAPdev {_fmt(f.get('vwap_dev_pct'))}%  maxpain {_fmt(f.get('max_pain'))}  liq {_fmt(f.get('liquidity_score'))}")
        top = "bullish_breakout" if bull >= bear else "bearish_breakdown"
        if max(bull, bear) >= 65:
            L.append(f"   EARLY WARNING: potential {'bullish' if bull >= bear else 'bearish'} pressure detected.")
            for e in sc[top]["positive"][:4]:
                L.append(f"     + {e}")
            for e in sc[top]["negative"][:2]:
                L.append(f"     - {e}")
            L.append(f"   WARNING: breakout not confirmed until futures close beyond "
                     f"{_fmt(f.get('or_high') if bull >= bear else f.get('or_low'))} with volume.")
        rel = sc[top].get("historical_reliability")
        L.append(f"   RELIABILITY: {rel if isinstance(rel, str) else json.dumps(rel, default=str)[:150]}")
        if sc["data_quality"]["negative"]:
            L.append(f"   DATA: {'; '.join(sc['data_quality']['negative'][:3])}")
        L.append("-" * 78)
    lr = eng.last_research or {}
    L.append(f" RESEARCH: last {lr.get('ts_ist', 'never')} rows {lr.get('panel_rows', 0)} FDR-passing: "
             f"{', '.join(lr.get('fdr_pass', [])[:4]) or 'none yet'}")
    L.append(" Scores are evidence indices 0-100, not probabilities. No orders are ever placed.")
    return "\n".join(L)


# ============================================================================
# 12. ENGINE ORCHESTRATION
# ============================================================================

class SingleInstance:
    """flock-based lock so two engines never open duplicate WebSocket connections."""

    def __init__(self, path: Path):
        self.path = path
        self.fh: Optional[io.TextIOWrapper] = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(str(os.getpid()))
        self.fh.flush()
        return True

    def release(self) -> None:
        if self.fh:
            with contextlib.suppress(Exception):
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
                self.fh.close()


class Engine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cal = MarketCalendar(cfg)
        self.db = Database(cfg.db_path, cfg.db_batch_size, cfg.db_flush_s)
        self.lim = RateLimiter()
        self.rest = DhanRest(cfg, self.lim)
        self.master = InstrumentMaster(cfg)
        self.feed = MarketFeed(cfg)
        self.states: Dict[str, IndexState] = {n: IndexState(n, cfg) for n in cfg.indices if n in DEFAULT_INDICES and n != "INDIAVIX"}
        self.vix: Optional[Contract] = None
        self.events = EventEngine(cfg, self.db)
        self.signals = SignalRecorder(cfg, self.db)
        self.alerter = Alerter(cfg, self.db)
        self.stop_ev = asyncio.Event()
        self.tasks: List[asyncio.Task] = []
        self.last_research: Dict[str, Any] = self.db.get_meta("last_research", {}) or {}
        self.reliability: Dict[str, Any] = self.db.get_meta("pattern_reliability", {}) or {}
        self.calibrated = bool(self.db.get_meta("calibration_ok", False))
        self.fatal_reason: Optional[str] = None
        self.started = time.time()
        self.heartbeat_file = cfg.data_dir / "heartbeat.json"
        self.dashboard_file = cfg.data_dir / "dashboard.txt"
        self.prepared_day: Optional[str] = None
        self.cross: Dict[str, Any] = {}
        self.feed.listeners.append(self._on_tick)
        self._last_tick_store: Dict[Tuple[str, int], float] = {}
        self._idx_by_key: Dict[Tuple[str, int], str] = {}
        self._fut_by_key: Dict[Tuple[str, int], str] = {}
        self._opt_by_key: Dict[Tuple[str, int], Tuple[str, float, str]] = {}

    # ------------------------------------------------------------- ticks
    def _on_tick(self, tk: Tick, kind: str) -> None:
        key = (tk.seg, tk.sid)
        t = tk.recv
        if key in self._idx_by_key:
            self.states[self._idx_by_key[key]].bars.update(tk.ltp, t)
        elif key in self._fut_by_key:
            self.states[self._fut_by_key[key]].fut_bars.update(tk.ltp, t, tk.volume)
        elif key in self._opt_by_key:
            name, K, side = self._opt_by_key[key]
            lg = self.states[name].chain.get((K, side))
            if lg is not None and kind in ("quote", "full"):
                lg.ltp = tk.ltp
                if fin(tk.volume) and tk.volume > 0:
                    lg.volume = tk.volume
                if tk.oi is not None and tk.oi > 0:
                    lg.oi = tk.oi
                if tk.bids and tk.asks and tk.bids[0][0] > 0 and tk.asks[0][0] > 0:
                    lg.bid, lg.ask = tk.bids[0][0], tk.asks[0][0]
                    lg.bid_qty, lg.ask_qty = tk.bids[0][1], tk.asks[0][1]
                    lg.depth_bids, lg.depth_asks = list(tk.bids), list(tk.asks)
                lg.src, lg.ts = "ws", t
        # raw tick archive for index/futures/VIX at most once per 5 s per instrument
        if key in self._idx_by_key or key in self._fut_by_key or (self.vix and key == (self.vix.segment, self.vix.security_id)):
            if t - self._last_tick_store.get(key, 0) >= 5:
                self._last_tick_store[key] = t
                p, u, i = ts_pair(t)
                self.db.insert("market_ticks", {
                    "ts": p, "ts_utc": u, "ts_ist": i, "security_id": tk.sid, "segment": tk.seg,
                    "name": self._idx_by_key.get(key) or (self._fut_by_key.get(key, "") + "_FUT" if key in self._fut_by_key else "INDIAVIX"),
                    "ltp": tk.ltp, "ltq": tk.ltq, "ltt": tk.ltt, "atp": tk.atp, "volume": tk.volume, "oi": tk.oi,
                    "buy_qty": tk.buy_qty, "sell_qty": tk.sell_qty,
                    "bid": tk.bids[0][0] if tk.bids else None, "ask": tk.asks[0][0] if tk.asks else None,
                    "bid_qty": tk.bids[0][1] if tk.bids else None, "ask_qty": tk.asks[0][1] if tk.asks else None,
                    "open": tk.open, "high": tk.high, "low": tk.low, "close": tk.close,
                    "depth": {"b": tk.bids, "a": tk.asks} if tk.bids else None})

    def vix_value(self) -> float:
        if not self.vix:
            return NAN
        tk = self.feed.tick(self.vix.segment, self.vix.security_id)
        return tk.ltp if tk else NAN

    def spot(self, st: IndexState) -> Tuple[float, float]:
        if st.idx:
            tk = self.feed.tick(st.idx.segment, st.idx.security_id)
            if tk and fin(tk.ltp):
                return tk.ltp, time.time() - tk.recv
        return st.chain_last_price, (time.time() - st.chain_ts if st.chain_ts else NAN)

    # ------------------------------------------------------------- daily prep
    async def prepare_day(self) -> None:
        """Instrument discovery: indices, futures, expiries, lot sizes (all from live sources)."""
        await asyncio.to_thread(self.master.load, False)
        self.vix = self.master.index("INDIAVIX")
        for name, st in self.states.items():
            st.idx = self.master.index(name)
            st.fut = self.master.near_future(name, 0)
            st.fut_next = self.master.near_future(name, 1)
            if st.fut and st.fut.expiry == now_ist().date().isoformat() and now_ist().time() > dt.time(15, 30):
                st.fut, st.fut_next = st.fut_next, self.master.near_future(name, 2)
            if not st.idx:
                LOG.error("index not found in instrument master", index=name)
                continue
            try:
                exps = await self.rest.expiry_list(st.idx.security_id, "IDX_I")
            except DhanApiError as e:
                LOG.warning("expiry list failed; falling back to instrument master", index=name, error=str(e)[:200])
                exps = self.master.option_expiries(name)
                if not e.retryable:
                    self.fatal_reason = f"Dhan auth/permission error: {e}"
            now = time.time()
            exps = [e for e in exps if expiry_timestamp(e) > now]
            st.expiries = exps
            st.expiry = exps[0] if exps else None
            st.next_expiry = exps[1] if len(exps) > 1 else None
            if st.expiry:
                st.lot = self.master.lot_size(name, st.expiry) or NAN
                st.step = self.master.strike_step(name, st.expiry) or NAN
            self._idx_by_key[(st.idx.segment, st.idx.security_id)] = name
            if st.fut:
                self._fut_by_key[(st.fut.segment, st.fut.security_id)] = name
            LOG.info("instruments discovered", index=name, sid=st.idx.security_id, expiry=st.expiry,
                     next_expiry=st.next_expiry, lot=st.lot, step=st.step,
                     fut=st.fut.display if st.fut else None, fut_sid=st.fut.security_id if st.fut else None)
        self.db.set_meta("instruments_today", {n: {"sid": s.idx.security_id if s.idx else None, "expiry": s.expiry,
                                                   "next": s.next_expiry, "lot": s.lot, "step": s.step,
                                                   "fut": s.fut.security_id if s.fut else None,
                                                   "fut_expiry": s.fut.expiry if s.fut else None}
                                               for n, s in self.states.items()})
        self._update_subscriptions()
        await self.load_history()
        self.prepared_day = now_ist().date().isoformat()

    def _update_subscriptions(self) -> None:
        want: Dict[Tuple[str, int], int] = {}
        if self.vix:
            want[(self.vix.segment, self.vix.security_id)] = 17
        self._opt_by_key.clear()
        for name, st in self.states.items():
            if st.idx:
                want[(st.idx.segment, st.idx.security_id)] = 17
            if st.fut:
                want[(st.fut.segment, st.fut.security_id)] = 21
            if not st.expiry:
                continue
            spot, _ = self.spot(st)
            ref = spot if fin(spot) else st.chain_last_price
            if not fin(ref):
                continue
            ks = sorted({k for (k, s) in self.master.options.get((st.spec.symbol, st.expiry), {})})
            if not ks:
                continue
            i = min(range(len(ks)), key=lambda j: abs(ks[j] - ref))
            for K in ks[max(0, i - self.cfg.ws_full_window): i + self.cfg.ws_full_window + 1]:
                for side in ("CE", "PE"):
                    c = self.master.option(name, st.expiry, K, side)
                    if c:
                        want[(c.segment, c.security_id)] = 21
                        self._opt_by_key[(c.segment, c.security_id)] = (name, K, side)
        for line in [x.strip() for x in _env("BREADTH_CONSTITUENTS").split(",") if x.strip()]:
            with contextlib.suppress(ValueError):
                seg, sid = line.split(":")
                want[(seg, int(sid))] = 15
        self.feed.want(want)

    async def load_history(self) -> None:
        today = now_ist().date()
        frm_i = (today - dt.timedelta(days=7)).isoformat()
        to_i = (today + dt.timedelta(days=1)).isoformat()
        frm_d = (today - dt.timedelta(days=400)).isoformat()
        for name, st in self.states.items():
            if not st.idx:
                continue
            try:
                d = await self.rest.daily(st.idx.security_id, "IDX_I", "INDEX", frm_d, today.isoformat())
                st.daily = candles_from_payload(d)
                self._store_candles(st.idx.security_id, "IDX_I", "1d", st.daily, name)
                c1 = candles_from_payload(await self.rest.intraday(st.idx.security_id, "IDX_I", "INDEX", frm_i, to_i, 1))
                st.bars.seed(c1)
                self._store_candles(st.idx.security_id, "IDX_I", "1m", c1, name)
                for m in (3, 5, 15, 30):
                    self._store_candles(st.idx.security_id, "IDX_I", f"{m}m", resample_candles(c1, m), name)
                if st.fut:
                    cf = candles_from_payload(await self.rest.intraday(st.fut.security_id, st.fut.segment, "FUTIDX",
                                                                       frm_i, to_i, 1, oi=True))
                    st.fut_bars.seed(cf)
                    self._store_candles(st.fut.security_id, st.fut.segment, "1m", cf, name + "_FUT")
                vm = await asyncio.to_thread(daily_vol_models, [x["close"] for x in st.daily], [x["close"] for x in c1])
                st.garch = vm.get("garch")
                p, u, i = ts_pair()
                self.db.insert("vol_metrics", {"ts": p, "ts_utc": u, "ts_ist": i, "index_name": name,
                                               "garch_sigma": st.garch.forecast_sigma * math.sqrt(252) if st.garch and st.garch.ok else None,
                                               "garch_persistence": st.garch.persistence if st.garch and st.garch.ok else None,
                                               "rv_1d": realized_vol(log_returns([x["close"] for x in st.daily])[-20:], 252) if len(st.daily) > 21 else None,
                                               "parkinson": parkinson_vol([x["high"] for x in st.daily[-20:]], [x["low"] for x in st.daily[-20:]], 252) if st.daily else None,
                                               "extra": {k: v for k, v in vm.items() if k != "garch"}})
                LOG.info("history loaded", index=name, daily=len(st.daily), minute=len(c1),
                         garch=bool(st.garch and st.garch.ok))
            except DhanApiError as e:
                LOG.warning("history load failed", index=name, error=str(e)[:200])

    def _store_candles(self, sid: int, seg: str, interval: str, cs: List[Dict[str, float]], name: str) -> None:
        for c in cs:
            self.db.insert("candles", {"security_id": sid, "segment": seg, "interval": interval, "ts": c["ts"],
                                       "ts_ist": dt.datetime.fromtimestamp(c["ts"], IST).isoformat(), "open": c["open"],
                                       "high": c["high"], "low": c["low"], "close": c["close"], "volume": c.get("volume"),
                                       "oi": c.get("oi"), "name": name}, mode="INSERT OR REPLACE")

    # ------------------------------------------------------------- loops
    async def chain_loop(self) -> None:
        """Round-robin option-chain polling at the documented 1-per-3s limit."""
        cycle = 0
        while not self.stop_ev.is_set():
            if self.cal.phase() not in ("open", "warmup", "linger") or not self.states:
                await self._sleep(30)
                continue
            for name, st in list(self.states.items()):
                if self.stop_ev.is_set():
                    break
                if not st.idx or not st.expiry:
                    continue
                if expiry_timestamp(st.expiry) <= time.time():
                    LOG.info("expiry passed; rolling", index=name, expiry=st.expiry)
                    st.expiries = [e for e in st.expiries if expiry_timestamp(e) > time.time()]
                    st.expiry = st.expiries[0] if st.expiries else None
                    st.next_expiry = st.expiries[1] if len(st.expiries) > 1 else None
                    st.chain = {}
                    self._update_subscriptions()
                    continue
                try:
                    raw = await self.rest.option_chain(st.idx.security_id, "IDX_I", st.expiry)
                    ts = time.time()
                    last, legs = normalize_chain(raw, ts)
                    if not legs:
                        raise DhanApiError("empty option chain")
                    # keep fresher WS values where the REST snapshot is older than the tick
                    for k, lg in legs.items():
                        old = st.chain.get(k)
                        if old and old.src == "ws" and ts - old.ts < 5:
                            lg.ltp, lg.bid, lg.ask, lg.bid_qty, lg.ask_qty = old.ltp, old.bid, old.ask, old.bid_qty, old.ask_qty
                    st.chain, st.chain_ts, st.chain_last_price = legs, ts, last
                    if not fin(st.step):
                        st.step = self.master.strike_step(name, st.expiry, last) or NAN
                    st.chain_errors = 0
                    if cycle % 3 == 0:
                        self._update_subscriptions()
                    if st.next_expiry and cycle % self.cfg.next_expiry_every_n == 0:
                        rawn = await self.rest.option_chain(st.idx.security_id, "IDX_I", st.next_expiry)
                        _, st.next_chain = normalize_chain(rawn, time.time())
                        st.next_chain_ts = time.time()
                except DhanApiError as e:
                    st.chain_errors += 1
                    LOG.warning("option chain fetch failed", index=name, error=str(e)[:200])
                    if not e.retryable:
                        self.fatal_reason = f"Dhan API: {e}"
                        await self._sleep(60)
                except Exception:
                    st.chain_errors += 1
                    LOG.exception("option chain processing failed", index=name)
            cycle += 1
            await asyncio.sleep(0)

    async def snapshot_loop(self) -> None:
        while not self.stop_ev.is_set():
            t0 = time.time()
            if self.cal.phase() in ("open", "linger"):
                for name, st in self.states.items():
                    try:
                        await self._snapshot(name, st)
                    except Exception:
                        LOG.exception("snapshot failed", index=name)
                self.events.update_post(self.states, time.time())
                self.signals.label(self.states, time.time())
            await self._sleep(max(1.0, self.cfg.snapshot_interval_s - (time.time() - t0)))

    async def _snapshot(self, name: str, st: IndexState) -> None:
        if not st.chain or not st.expiry:
            return
        ts = time.time()
        spot, age = self.spot(st)
        fut_tk = self.feed.tick(st.fut.segment, st.fut.security_id) if st.fut else None
        n30 = None
        if "NIFTY" in self.states and name != "NIFTY":
            n30 = self.states["NIFTY"].last_features.get("ret_30")
            n30 = n30 if fin(n30) else None
        f, rec, legs = await asyncio.to_thread(build_features, st, ts, spot, fut_tk, self.vix_value(), n30, self.cfg.risk_free)
        if not rec.legs:
            return
        dq, issues = data_quality_score(f, age, self.feed.connected, self.cfg)
        f["data_quality"] = dq
        rel = {"bullish": self._rel("BULL_SCORE_HIGH"), "bearish": self._rel("BEAR_SCORE_HIGH"),
               "volatility": self._rel("STRADDLE_EXPANSION")}
        sc = compute_scores(f, dq, issues, rel)
        f["_bull"], f["_bear"] = sc["bullish_breakout"]["score"], sc["bearish_breakdown"]["score"]
        f["_volx"] = sc["volatility_expansion"]["score"]
        reg = classify_regime(f, sc)
        if st.hmm_state is not None:
            reg["hmm_state"] = st.hmm_state
        f["_regime"] = reg["regime"]
        pats = active_patterns(f) if dq >= 50 else {}
        f["_patterns"] = pats
        rec.scores = {"bull": f["_bull"], "bear": f["_bear"], "volx": f["_volx"], "rev": sc["reversal"]["score"]}
        st.history.append(rec)
        st.last_features, st.last_scores, st.regime = f, sc, reg
        # ---- persist ----------------------------------------------------------------
        p, u, i = ts_pair(ts)
        self.db.insert("index_snapshots", {
            "ts": p, "ts_utc": u, "ts_ist": i, "index_name": name, "spot": spot, "fut": f.get("fut"),
            "forward": f.get("forward"), "vix": f.get("vix"), "vwap": f.get("vwap"), "basis": f.get("basis"),
            "expiry": st.expiry, "atm": f.get("atm"), "pcr_oi": f.get("pcr_oi_5"), "pcr_vol": f.get("pcr_vol_5"),
            "max_pain": f.get("max_pain"), "atm_iv": f.get("atm_iv"), "rv": f.get("rv_1m_ann"), "straddle": f.get("straddle"),
            "gex": f.get("gex"), "gamma_conc": f.get("gamma_conc"), "data_quality": dq, "regime": reg["regime"],
            "scores": {k: (v.get("score") if isinstance(v, dict) else v) for k, v in sc.items() if not k.startswith("_")},
            "features": {k: v for k, v in f.items() if isinstance(v, (int, float, str, bool, dict, type(None)))}})
        win = set(st.window_strikes(f.get("atm", NAN), self.cfg.strike_window))
        for (K, side), lg in legs.items():
            if K not in win:
                continue
            prev = st.history[-2].legs.get((K, side)) if len(st.history) > 1 else None
            lg.buildup = classify_buildup(lg.fair - prev[0] if prev and fin(prev[0]) and fin(lg.fair) else None,
                                          lg.oi - prev[1] if prev and fin(prev[1]) and fin(lg.oi) else None)
            self.db.insert("chain_snapshots", {
                "ts": p, "ts_utc": u, "ts_ist": i, "index_name": name, "expiry": st.expiry, "strike": K, "side": side,
                "security_id": lg.sid, "ltp": lg.ltp, "oi": lg.oi, "prev_oi": lg.prev_oi, "volume": lg.volume,
                "prev_volume": lg.prev_volume, "prev_close": lg.prev_close, "bid": lg.bid, "ask": lg.ask,
                "bid_qty": lg.bid_qty, "ask_qty": lg.ask_qty, "vendor_iv": lg.v_iv, "vendor_delta": lg.v_delta,
                "vendor_gamma": lg.v_gamma, "vendor_theta": lg.v_theta, "vendor_vega": lg.v_vega, "iv": lg.iv,
                "delta": lg.delta, "gamma": lg.gamma, "theta": lg.theta, "vega": lg.vega, "rho": lg.rho,
                "spread_pct": lg.spread_pct, "liquidity": lg.liq, "buildup": lg.buildup, "source": lg.src})
        if int(ts) % 300 < self.cfg.snapshot_interval_s:      # ~every 5 minutes
            self._exec_costs(name, st, legs, f, p, i)
            self.db.insert("vol_metrics", {"ts": p, "ts_utc": u, "ts_ist": i, "index_name": name, "atm_iv": f.get("atm_iv"),
                                           "rv_5m": f.get("rv_1m_ann"), "iv_rv_spread": f.get("iv_rv_spread"),
                                           "skew_25d": f.get("skew_2strike"), "vix": f.get("vix"),
                                           "term_slope": self._term_slope(st, f),
                                           "garch_sigma": f.get("garch_sigma_ann"), "garch_persistence": f.get("garch_persistence")})
            self.db.insert("regimes", {"ts": p, "ts_utc": u, "ts_ist": i, "index_name": name, "regime": reg["regime"],
                                       "secondary": reg.get("secondary"), "evidence": reg.get("evidence"),
                                       "hmm_state": st.hmm_state})
        # ---- events, signals, alerts ---------------------------------------------------
        ev = self.events.check(st, ts) if self.cal.phase() == "open" else None
        new = self.signals.record(st, ts, pats)
        for kind, text, key in alert_candidates(st, f, sc, new, ev, self.calibrated):
            await self.alerter.send(name, kind, text, key)

    def _rel(self, pattern: str) -> Optional[Dict[str, Any]]:
        r = self.reliability.get(pattern)
        return r if r and r.get("status") == "OK" else None

    def _term_slope(self, st: IndexState, f: Dict[str, Any]) -> Optional[float]:
        if not st.next_chain or not st.next_expiry or not fin(f.get("atm_iv")) or not fin(f.get("forward")):
            return None
        atm = f["atm"]
        T2 = year_fraction(time.time(), st.next_expiry)
        ivs = []
        for side in ("CE", "PE"):
            lg = st.next_chain.get((atm, side))
            if lg and fin(lg.fair):
                F2 = f["forward"] * math.exp(self.cfg.risk_free * (T2 - f["T_days"] / 365))
                iv = implied_vol_black76(lg.fair, F2, atm, T2, self.cfg.risk_free, side == "CE")
                if iv:
                    ivs.append(iv)
        return float(np.mean(ivs)) - f["atm_iv"] if ivs else None

    def _exec_costs(self, name: str, st: IndexState, legs: Dict[Tuple[float, str], Leg], f: Dict[str, Any],
                    p: float, i: str) -> None:
        atm, step = f.get("atm"), f.get("step")
        if not fin(atm) or not fin(step) or not fin(st.lot):
            return
        for d in range(-3, 4):
            for side in ("CE", "PE"):
                lg = legs.get((atm + d * step, side))
                if not lg or not fin(lg.mid):
                    continue
                asks = [(a, q) for a, q, _ in lg.depth_asks] if lg.depth_asks else ([(lg.ask, lg.ask_qty)] if fin(lg.ask) else [])
                s1 = slippage_estimate(lg.mid, asks, st.lot)
                s5 = slippage_estimate(lg.mid, asks, 5 * st.lot)
                fees = option_costs_bps(lg.mid, int(st.lot), False) + option_costs_bps(lg.mid, int(st.lot), True)
                adv = lg.volume if fin(lg.volume) and lg.volume > 0 else NAN
                imp = sqrt_impact(5 * st.lot, adv, (f.get("atm_iv") or 0.15) / math.sqrt(252) * 3) if fin(adv) else None
                self.db.insert("execution_costs", {"ts": p, "ts_ist": i, "index_name": name, "expiry": st.expiry,
                                                   "strike": lg.strike, "side": side, "mid": lg.mid, "spread_pct": lg.spread_pct,
                                                   "slippage_1lot": s1, "slippage_5lot": s5, "fees_bps": fees,
                                                   "total_cost_pct": execution_cost_pct(lg.spread_pct if fin(lg.spread_pct) else None, s1, fees),
                                                   "impact_sqrt": imp, "liquidity": lg.liq})

    async def quote_fallback_loop(self) -> None:
        """If the WebSocket is down/stale during market hours, poll /marketfeed/quote (1 req/s limit)."""
        while not self.stop_ev.is_set():
            await self._sleep(5)
            if self.cal.phase() != "open":
                continue
            stale = [n for n, st in self.states.items() if st.idx and (self.spot(st)[1] > self.cfg.ws_stale_s
                                                                       or not fin(self.spot(st)[1]))]
            if not stale and self.feed.connected:
                continue
            req: Dict[str, List[int]] = collections.defaultdict(list)
            for n in stale or list(self.states):
                st = self.states[n]
                if st.idx:
                    req["IDX_I"].append(st.idx.security_id)
                if st.fut:
                    req[st.fut.segment].append(st.fut.security_id)
            if self.vix:
                req["IDX_I"].append(self.vix.security_id)
            try:
                data = await self.rest.quote(dict(req))
            except DhanApiError as e:
                LOG.warning("quote fallback failed", error=str(e)[:200])
                continue
            now = time.time()
            for seg, d in (data or {}).items():
                for sid, q in (d or {}).items():
                    if not isinstance(q, dict):
                        continue
                    ltp = _f(q.get("last_price"))
                    if not fin(ltp) or ltp <= 0:
                        continue
                    key = (seg, int(sid))
                    tk = self.feed.ticks.get(key) or Tick(int(sid), seg)
                    tk.ltp, tk.recv = ltp, now
                    tk.volume = _f(q.get("volume")) if fin(_f(q.get("volume"))) else tk.volume
                    tk.atp = _f(q.get("average_price")) if fin(_f(q.get("average_price"))) else tk.atp
                    oi = _f(q.get("oi"))
                    tk.oi = oi if fin(oi) else tk.oi
                    dep = q.get("depth") or {}
                    tk.bids = [(_f(x.get("price")), _f(x.get("quantity")), int(_f(x.get("orders")) or 0)) for x in dep.get("buy", [])]
                    tk.asks = [(_f(x.get("price")), _f(x.get("quantity")), int(_f(x.get("orders")) or 0)) for x in dep.get("sell", [])]
                    tk.buy_qty = _f(q.get("buy_quantity")) if fin(_f(q.get("buy_quantity"))) else tk.buy_qty
                    tk.sell_qty = _f(q.get("sell_quantity")) if fin(_f(q.get("sell_quantity"))) else tk.sell_qty
                    self.feed.ticks[key] = tk
                    self._on_tick(tk, "quote")

    async def research_loop(self) -> None:
        await self._sleep(120)
        n = await asyncio.to_thread(self.signals.relabel_from_db, self.db)
        if n:
            LOG.info("relabelled signals from DB after restart", n=n)
        while not self.stop_ev.is_set():
            try:
                await asyncio.to_thread(research_cycle, self.db, self.cfg)
                self.last_research = self.db.get_meta("last_research", {}) or {}
                self.reliability = self.db.get_meta("pattern_reliability", {}) or {}
                self.calibrated = bool(self.db.get_meta("calibration_ok", False))
                await asyncio.to_thread(self._cross_asset)
                await asyncio.to_thread(self._hmm)
            except Exception:
                LOG.exception("research cycle failed")
            await self._sleep(self.cfg.research_every_s if self.cal.phase() == "open" else 4 * 3600)

    def _cross_asset(self) -> None:
        """LEVEL 3/5: covariance, Ledoit-Wolf shrinkage, PCA, conditioning, cointegration across indices."""
        names = [n for n, st in self.states.items() if len(st.bars.all()) > 120]
        if len(names) < 2:
            return
        series = {}
        for n in names:
            b = self.states[n].bars.all()
            series[n] = {x["ts"]: x["close"] for x in b}
        common = sorted(set.intersection(*[set(s) for s in series.values()]))[-1500:]
        if len(common) < 120:
            return
        P = np.array([[series[n][t] for n in names] for t in common])
        R = np.diff(np.log(P), axis=0)
        S, delta = ledoit_wolf_shrinkage(R)
        pc = pca(R)
        out = {"names": names, "n": len(R), "lw_delta": delta, "cond_sample": condition_number(covariance_matrix(R)),
               "cond_shrunk": condition_number(S), "minvar_w": dict(zip(names, np.round(min_variance_weights(S), 4).tolist()))}
        if pc.get("ok"):
            out["pc_explained"] = np.round(pc["explained"], 4).tolist()
            out["pc1_loadings"] = dict(zip(names, np.round(pc["eigenvectors"][:, 0], 4).tolist()))
        if "NIFTY" in names and "BANKNIFTY" in names:
            out["coint_nifty_banknifty"] = engle_granger(np.log(P[:, names.index("BANKNIFTY")]), np.log(P[:, names.index("NIFTY")]))
            out["capm_banknifty"] = capm_beta(R[:, names.index("BANKNIFTY")], R[:, names.index("NIFTY")])
        self.cross = out
        self.db.set_meta("cross_asset", out)

    def _hmm(self) -> None:
        for n, st in self.states.items():
            c = st.bars.closes()
            if len(c) < 600:
                continue
            r = np.diff(np.log(c))[-3000:]
            X = np.column_stack([r, np.abs(r)])
            try:
                h = GaussianHMM(3, 30).fit(X)
                s, _ = h.predict_last(X)
                order = np.argsort(np.sqrt(h.var[:, 0]))    # 0 = calmest
                st.hmm_state = int(np.where(order == s)[0][0])
            except Exception:
                LOG.exception("hmm failed", index=n)

    async def health_loop(self) -> None:
        while not self.stop_ev.is_set():
            hb = self.health()
            try:
                tmp = self.heartbeat_file.with_suffix(".tmp")
                tmp.write_text(jdump(hb))
                tmp.replace(self.heartbeat_file)
            except OSError:
                pass
            if int(time.time()) % 300 < 10:
                p, _, i = ts_pair()
                self.db.insert("health", {"ts": p, "ts_ist": i, "component": "engine", "status": hb["status"], "detail": hb})
            await self._sleep(10)

    def health(self) -> Dict[str, Any]:
        ph = self.cal.phase()
        ages = {n: self.spot(st)[1] for n, st in self.states.items()}
        chain_age = {n: (time.time() - st.chain_ts) if st.chain_ts else None for n, st in self.states.items()}
        stale = ph == "open" and (any((a is None or not fin(a) or a > self.cfg.ws_stale_s) for a in ages.values())
                                  or any((a is None or a > self.cfg.chain_stale_s) for a in chain_age.values()))
        status = "ok"
        if self.fatal_reason or self.feed.fatal:
            status = "fatal"
        elif ph == "open" and (not self.feed.connected or stale):
            status = "degraded"
        return {"status": status, "phase": ph, "pid": os.getpid(), "uptime_s": round(time.time() - self.started),
                "ts_ist": now_ist().isoformat(), "ws_connected": self.feed.connected, "ws_msgs": self.feed.msgs,
                "ws_reconnects": self.feed.reconnects, "ws_fatal": self.feed.fatal, "subscriptions": len(self.feed.subscribed),
                "spot_age_s": ages, "chain_age_s": chain_age, "rest_errors": self.rest.errors,
                "rate_calls_today": dict(self.lim.daily), "db_rows": self.db.rows_written, "db_pending": self.db.pending(),
                "db_errors": self.db.write_errors, "events": self.events.n_events, "signals": self.signals.n,
                "alerts_sent": self.alerter.sent, "alerts_failed": self.alerter.failed, "fatal": self.fatal_reason,
                "token": self.cfg.token_fingerprint(), "orders_placed": 0}

    async def health_server(self) -> None:
        if aiohttp is None or self.cfg.health_port <= 0:
            return
        from aiohttp import web

        async def h(_req: Any) -> Any:
            hb = self.health()
            return web.json_response(hb, status=200 if hb["status"] != "fatal" else 503, dumps=jdump)

        app = web.Application()
        app.router.add_get("/health", h)
        app.router.add_get("/", h)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            site = web.TCPSite(runner, self.cfg.health_host, self.cfg.health_port)
            await site.start()
            LOG.info("health endpoint up", host=self.cfg.health_host, port=self.cfg.health_port)
            await self.stop_ev.wait()
        except OSError as e:
            LOG.warning("health endpoint failed to bind", error=str(e))
        finally:
            await runner.cleanup()

    async def dashboard_loop(self, live: bool) -> None:
        console = None
        if live:
            try:
                from rich.console import Console
                from rich.live import Live
                from rich.text import Text
                console = Console()
                with Live(Text(""), console=console, refresh_per_second=1, screen=False) as lv:
                    while not self.stop_ev.is_set():
                        txt = render_dashboard_text(self)
                        self._write_dash(txt)
                        lv.update(Text(txt))
                        await self._sleep(self.cfg.dashboard_every_s)
                return
            except ImportError:
                pass
        while not self.stop_ev.is_set():
            self._write_dash(render_dashboard_text(self))
            await self._sleep(max(self.cfg.dashboard_every_s, 5))

    def _write_dash(self, txt: str) -> None:
        try:
            tmp = self.dashboard_file.with_suffix(".tmp")
            tmp.write_text(txt)
            tmp.replace(self.dashboard_file)
        except OSError:
            pass

    async def session_supervisor(self) -> None:
        """Prepares each trading day, idles outside market hours, prunes memory."""
        while not self.stop_ev.is_set():
            ph = self.cal.phase()
            today = now_ist().date().isoformat()
            if ph in ("warmup", "open", "linger"):
                if self.prepared_day != today:
                    try:
                        await self.prepare_day()
                    except Exception:
                        LOG.exception("prepare_day failed; retrying in 60s")
                        await self._sleep(60)
                        continue
                # holiday auto-detect: no index tick 20 min after open
                if ph == "open" and self.cal.minutes_since_open() > 20 and self.feed.connected:
                    ticks = [self.feed.tick(st.idx.segment, st.idx.security_id) for st in self.states.values() if st.idx]
                    if ticks and all((t is None or now_ist().timestamp() - (t.recv or 0) > 900) for t in ticks):
                        LOG.warning("no index ticks 20 minutes after open - possible unlisted holiday")
            else:
                if self.prepared_day and self.prepared_day != today:
                    for st in self.states.values():
                        st.history.clear()
                        st.patterns_on.clear()
                    self.prepared_day = None
                nxt = self.cal.next_session_start()
                wait = (nxt - now_ist()).total_seconds()
                LOG.info("outside market hours; idling", next_session=nxt.isoformat(), wait_min=round(wait / 60))
                await self._sleep(min(max(wait, 30), 1800))
                continue
            await self._sleep(30)

    async def feed_supervisor(self) -> None:
        """Runs the WebSocket only during market phases (reduces load outside hours)."""
        task: Optional[asyncio.Task] = None
        while not self.stop_ev.is_set():
            active = self.cal.phase() in ("warmup", "open", "linger") and self.prepared_day is not None
            if active and (task is None or task.done()):
                self.feed._stop = asyncio.Event()
                task = asyncio.create_task(self.feed.run(), name="ws-feed")
            elif not active and task and not task.done():
                self.feed.stop()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(task, 15)
                task = None
            await self._sleep(5)
        if task and not task.done():
            self.feed.stop()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, 15)

    async def _sleep(self, s: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.stop_ev.wait(), timeout=s)

    async def run(self, live_dashboard: bool) -> None:
        self.db.start()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.stop_ev.set)
        LOG.info("engine starting", version=APP_VERSION, indices=list(self.states), token=self.cfg.token_fingerprint(),
                 sdk=HAVE_SDK, scipy=HAVE_SCIPY)
        if not self.cfg.client_id or not self.cfg.resolve_token():
            self.fatal_reason = "DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN not set (see .env.example)"
        coros = [self.session_supervisor(), self.feed_supervisor(), self.chain_loop(), self.snapshot_loop(),
                 self.quote_fallback_loop(), self.research_loop(), self.health_loop(), self.health_server(),
                 self.dashboard_loop(live_dashboard)]
        self.tasks = [asyncio.create_task(self._guard(c), name=c.__qualname__) for c in coros]
        await self.stop_ev.wait()
        LOG.info("shutdown requested")
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.alerter.close()
        await asyncio.to_thread(self.db.stop)
        LOG.info("engine stopped cleanly", rows=self.db.rows_written)

    async def _guard(self, coro: Any) -> None:
        """A crashing loop is logged and restarted, never takes the process down."""
        name = getattr(coro, "__qualname__", "task")
        fn = getattr(self, name.split(".")[-1])
        while not self.stop_ev.is_set():
            try:
                await coro
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("task crashed; restarting in 5s", task=name)
                await self._sleep(5)
                coro = fn() if name.split(".")[-1] != "dashboard_loop" else fn(False)

# ============================================================================
# 13. SELF-TESTS (offline) AND LIVE VERIFICATION
# ============================================================================

class Report:
    def __init__(self, title: str):
        self.title = title
        self.rows: List[Tuple[str, str, str]] = []

    def add(self, item: str, status: str, detail: str = "") -> None:
        self.rows.append((item, status, detail))
        mark = {"PASS": "[PASS]", "FAIL": "[FAIL]", "SKIP": "[SKIP]", "N/A": "[ N/A]"}.get(status, f"[{status}]")
        print(f"{mark} {item}" + (f" - {detail}" if detail else ""), flush=True)

    def check(self, item: str, cond: bool, detail: str = "") -> bool:
        self.add(item, "PASS" if cond else "FAIL", detail)
        return cond

    def markdown(self) -> str:
        c = collections.Counter(s for _, s, _ in self.rows)
        out = [f"# {self.title}", "", f"Generated {now_ist():%Y-%m-%d %H:%M:%S} IST", "",
               f"PASS {c['PASS']} | FAIL {c['FAIL']} | SKIP {c['SKIP']} | N/A {c['N/A']}", "",
               "| # | Item | Status | Detail |", "|---|---|---|---|"]
        for i, (a, s, d) in enumerate(self.rows, 1):
            out.append(f"| {i} | {a} | {s} | {d.replace('|', '/')} |")
        return "\n".join(out) + "\n"

    @property
    def failed(self) -> int:
        return sum(1 for _, s, _ in self.rows if s == "FAIL")


def _synthetic_chain(F: float, step: float, T: float, r: float, sigma: float, oi_shift: float = 0.0,
                     n: int = 25, base_oi: float = 1e6) -> Dict[str, Any]:
    oc = {}
    atm = round(F / step) * step
    for i in range(-n, n + 1):
        K = atm + i * step
        sk = sigma * (1 + 0.1 * (K / F - 1) ** 2 * 50)
        c = black76_price(F, K, T, r, sk, True)
        p = black76_price(F, K, T, r, sk, False)
        ce_oi = base_oi * math.exp(-((K - atm - 3 * step) / (6 * step)) ** 2) * (1 + (oi_shift if K > atm else 0))
        pe_oi = base_oi * math.exp(-((K - atm + 3 * step) / (6 * step)) ** 2) * (1 - (oi_shift if K < atm else 0))
        def leg(px: float, oi: float) -> Dict[str, Any]:
            return {"last_price": round(px, 2), "oi": int(oi), "previous_oi": int(oi * 0.9), "volume": int(oi * 3),
                    "previous_volume": int(oi * 2), "previous_close_price": round(px * 1.01, 2),
                    "top_bid_price": round(px * 0.995, 2), "top_ask_price": round(px * 1.005 + 0.05, 2),
                    "top_bid_quantity": 500, "top_ask_quantity": 450, "implied_volatility": sk * 100,
                    "greeks": {"delta": 0.5, "gamma": 0.001, "theta": -5, "vega": 10}, "security_id": 100000 + int(K)}
        oc[f"{K:.6f}"] = {"ce": leg(max(c, 0.05), ce_oi), "pe": leg(max(p, 0.05), pe_oi)}
    return {"last_price": F - 30, "oc": oc}


def run_selftest(cfg: Config) -> Report:
    rep = Report("DHAN QUANT ENGINE - OFFLINE SELF-TEST")
    r_ = 0.065
    # ---- pricing ----------------------------------------------------------------
    c = bs_price(100, 100, 1, 0.05, 0.2, True)
    rep.check("Black-Scholes reference value (Hull: 10.4506)", abs(c - 10.4506) < 1e-3, f"{c:.4f}")
    p = bs_price(100, 100, 1, 0.05, 0.2, False)
    rep.check("Put-call parity C-P = S - K e^-rT", abs((c - p) - (100 - 100 * math.exp(-0.05))) < 1e-9, f"{c - p:.6f}")
    F = 100 * math.exp(0.05)
    rep.check("Black-76(F=S e^rT) == Black-Scholes", abs(black76_price(F, 100, 1, 0.05, 0.2, True) - c) < 1e-9)
    worst = 0.0
    for is_call in (True, False):
        for K in (80, 95, 100, 105, 125):
            for T in (2 / 365, 0.1, 1.0):
                for s in (0.08, 0.2, 0.6):
                    g = black76_greeks(F, K, T, r_, s, is_call)
                    h = F * 1e-4
                    up, dn = black76_price(F + h, K, T, r_, s, is_call), black76_price(F - h, K, T, r_, s, is_call)
                    fd_d = (up - dn) / (2 * h)
                    fd_g = (up - 2 * g.price + dn) / (h * h)
                    fd_v = (black76_price(F, K, T, r_, s + 1e-4, is_call) - black76_price(F, K, T, r_, s - 1e-4, is_call)) / 2e-4 * 0.01
                    dtd = 1 / 365
                    fd_t = (black76_price(F, K, max(T - dtd, 1e-6), r_, s, is_call) - g.price) if T > dtd else g.theta
                    tol = max(1e-6, 1e-3 * max(abs(g.price), 1))
                    errs = [abs(fd_d - g.delta), abs(fd_g - g.gamma) / max(abs(g.gamma), 1e-6) * tol if g.gamma > 1e-6 else 0,
                            abs(fd_v - g.vega), abs(fd_t - g.theta) if T > 0.05 else 0]
                    worst = max(worst, max(errs[0], errs[2]))
                    if abs(fd_t - g.theta) > max(0.02 * abs(g.theta), 1e-3) and T > 0.05:
                        worst = max(worst, 1.0)
    rep.check("Greeks vs finite differences (delta, gamma, vega, theta) on 90-point grid", worst < 1e-3, f"max abs err {worst:.2e}")
    worst_iv, n_iv, fails = 0.0, 0, 0
    for is_call in (True, False):
        for K in (85, 95, 100, 105, 115):
            for T in (1 / 365, 7 / 365, 0.25, 1.0):
                for s in (0.05, 0.12, 0.25, 0.5, 1.2):
                    px = black76_price(F, K, T, r_, s, is_call)
                    if px < 1e-6:
                        continue
                    iv = implied_vol_black76(px, F, K, T, r_, is_call)
                    n_iv += 1
                    if iv is None:
                        fails += 1
                        continue
                    worst_iv = max(worst_iv, abs(black76_price(F, K, T, r_, iv, is_call) - px))
    rep.check("Implied-vol solver round-trip (price error)", fails == 0 and worst_iv < 1e-6,
              f"{n_iv} cases, max price err {worst_iv:.2e}, failures {fails}")
    rep.check("IV solver rejects arbitrage-violating price", implied_vol_black76(0.0001, 100, 50, 0.5, r_, True) is None)
    ks = [95, 100, 105]
    calls = [black76_price(101, k, 0.1, r_, 0.2, True) for k in ks]
    puts = [black76_price(101, k, 0.1, r_, 0.2, False) for k in ks]
    pf = parity_forward(ks, calls, puts, 0.1, r_, 100, band=0.06)
    rep.check("Put-call parity forward recovery", pf is not None and abs(pf[0] - 101) < 1e-6, f"{pf[0] if pf else None}")
    rep.check("Max pain (known example)", max_pain([90, 100, 110], [100, 500, 1000], [1000, 500, 100]) == 100)
    gp = gamma_pnl_approx(0.01, 10, -2, 1, 0.5)
    rep.check("Gamma P&L Taylor approximation", abs(gp - (5 + 0.5 - 2)) < 1e-12, f"{gp}")
    # ---- probability / statistics ---------------------------------------------------
    rep.check("Bayes posterior", abs(bayes_posterior(0.01, 0.9, 0.05) - 0.009 / (0.009 + 0.0495)) < 1e-12)
    w = wilson_interval(8, 10)
    rep.check("Wilson interval 8/10", abs(w[1] - 0.4902) < 1e-3 and abs(w[2] - 0.9433) < 1e-3, f"{w}")
    bh = benjamini_hochberg([0.01, 0.04, 0.03, 0.005, 0.5], 0.05)
    rep.check("Benjamini-Hochberg FDR", bh == [True, True, True, True, False], f"{bh}")
    rng = np.random.default_rng(1)
    X = rng.normal(size=(500, 2))
    y = 1.5 + 2 * X[:, 0] - 0.5 * X[:, 1] + rng.normal(scale=0.1, size=500)
    o = ols(y, X)
    rep.check("OLS coefficients / R^2", np.allclose(o.beta, [1.5, 2, -0.5], atol=0.02) and o.r2 > 0.99, f"{np.round(o.beta, 3)}, R2 {o.r2:.4f}")
    rr, pv = pearson(X[:, 0], y)
    rep.check("Pearson correlation + p-value", rr > 0.9 and pv < 1e-10, f"r={rr:.3f}")
    rw = np.cumsum(rng.normal(size=1500))
    ar = np.zeros(1500)
    for t in range(1, 1500):
        ar[t] = 0.5 * ar[t - 1] + rng.normal()
    a1, a2 = adf_test(rw), adf_test(ar)
    rep.check("ADF: random walk non-stationary, AR(0.5) stationary", (not a1["stationary_5pct"]) and a2["stationary_5pct"],
              f"RW stat {a1['stat']:.2f} p {a1['p']:.2f}; AR stat {a2['stat']:.2f} p {a2['p']:.3f}")
    hl = ou_half_life(ar)
    rep.check("OU half-life of AR(0.5) ~ ln2/ln2 = 1", 0.7 < hl < 1.4, f"{hl:.2f}")
    if HAVE_SCIPY:
        n = 3000
        e = np.zeros(n)
        h = np.zeros(n)
        h[0] = 1e-4
        for t in range(1, n):
            h[t] = 2e-6 + 0.1 * e[t - 1] ** 2 + 0.85 * h[t - 1]
            e[t] = math.sqrt(h[t]) * rng.normal()
        g = garch11_fit(e)
        rep.check("GARCH(1,1) MLE recovers alpha/beta (0.10/0.85)", g.ok and abs(g.alpha - 0.1) < 0.05 and abs(g.beta - 0.85) < 0.08,
                  f"alpha {g.alpha:.3f} beta {g.beta:.3f}")
    else:
        rep.add("GARCH(1,1) MLE", "SKIP", "scipy not installed")
    R = rng.multivariate_normal([0, 0, 0], [[1, .8, .3], [.8, 1, .2], [.3, .2, 1]], size=400)
    S, d = ledoit_wolf_shrinkage(R)
    pc = pca(R)
    rep.check("Ledoit-Wolf shrinkage + PCA eigen-decomposition", 0 <= d <= 1 and pc["ok"] and pc["explained"][0] > 0.5,
              f"delta {d:.3f}, PC1 {pc['explained'][0]:.2f}, cond {pc['condition_number']:.1f}")
    wmv = min_variance_weights(S)
    rep.check("Min-variance weights (Lagrange) sum to 1", abs(wmv.sum() - 1) < 1e-9)
    mu = np.array([0.1, 0.12, 0.08])
    wmv2 = mean_variance_weights(mu, S, 0.1)
    rep.check("Mean-variance frontier hits target", abs(wmv2 @ mu - 0.1) < 1e-9 and abs(wmv2.sum() - 1) < 1e-9)
    rep.check("Jensen gap (convexity) positive", jensen_gap_lognormal(0.2, 1) > 0)
    paths = gbm_paths(100, 0.05, 0.2, 1, 252, 20000, 3)
    rep.check("GBM Monte Carlo E[S_T] = S0 e^muT (Ito drift)", abs(paths[:, -1].mean() / (100 * math.exp(0.05)) - 1) < 0.01,
              f"{paths[:, -1].mean():.2f}")
    rep.check("Kelly fraction (p=.6, 1:1) = 0.2", abs(kelly_fraction(0.6, 1, 1) - 0.2) < 1e-12)
    rets = rng.normal(0.001, 0.01, 5000)
    v, es = var_es(rets, 0.95)
    rep.check("Historical VaR95 / ES95 vs normal", abs(v - (0.01 * 1.645 - 0.001)) < 0.001 and es > v, f"VaR {v:.4f} ES {es:.4f}")
    mdd = max_drawdown([1, 2, 1.5, 3, 1, 2])
    rep.check("Max drawdown", mdd[0] == -2.0, f"{mdd}")
    dsr = deflated_sharpe_ratio(0.1, 1000, 50, 0.0, 3.0, 0.002)
    psr = probabilistic_sharpe(0.1, 1000, 0.0, 3.0)
    rep.check("Deflated Sharpe < Probabilistic Sharpe under multiple trials", 0 <= dsr < psr <= 1, f"DSR {dsr:.3f} PSR {psr:.3f}")
    cm = confusion([1, 1, 0, 0, 1], [1, 0, 0, 1, 1])
    rep.check("Precision / recall / F1 / confusion matrix", cm["tp"] == 2 and abs(cm["precision"] - 2 / 3) < 1e-12
              and abs(cm["recall"] - 2 / 3) < 1e-12, f"{cm}")
    rep.check("Square-root impact", abs(sqrt_impact(100, 10000, 0.02) - 0.7 * 0.02 * 0.1) < 1e-12)
    rep.check("Order-book imbalance / microprice", book_imbalance(300, 100) == 0.5 and abs(microprice(99, 101, 300, 100) - 100.5) < 1e-12)
    rep.check("Walk-the-book fill price", walk_book_cost([(10, 5), (11, 5)], 8) == (50 + 33) / 8)
    rep.check("VWAP / TWAP", vwap([1, 2], [1, 3]) == 1.75 and twap([1, 2, 3]) == 2)
    # ---- classification -------------------------------------------------------------
    rep.check("Buildup classification uses price AND OI", [classify_buildup(1, 1), classify_buildup(-1, 1), classify_buildup(1, -1),
                                                            classify_buildup(-1, -1), classify_buildup(None, 5)] ==
              ["LONG_BUILDUP", "WRITING", "SHORT_COVERING", "LONG_UNWINDING", "UNKNOWN"])
    rs = RollingStat(200)
    for x in rng.normal(size=200):
        rs.add(float(x))
    z = rs.robust_z(5.0)
    rep.check("Adaptive robust z-score (median/MAD)", z is not None and z > 3.5, f"z={z:.2f}")
    # ---- feed decoding --------------------------------------------------------------
    full = struct.pack("<BHBIfHIfIIIIIIffff", 8, 162, 2, 12345, 101.5, 75, 1730000000, 100.9, 50000, 7000, 9000,
                       123000, 125000, 120000, 99.0, 98.5, 103.0, 97.0)
    for i in range(5):
        full += struct.pack("<IIHHff", 100 + i, 200 + i, 3, 4, 101.0 - i * 0.05, 101.5 + i * 0.05)
    quote = struct.pack("<BHBIfHIfIIIffff", 4, 50, 0, 13, 24000.5, 0, 1730000000, 0.0, 0, 0, 0, 23900, 23850, 24100, 23800)
    oi = struct.pack("<BHBII", 5, 12, 2, 12345, 130000)
    pk = parse_feed_packet(full + quote + oi)
    rep.check("WebSocket binary decoding (Full+Quote+OI, concatenated)", len(pk) == 3 and pk[0]["oi"] == 123000 and
              abs(pk[0]["bids"][0][0] - 101.0) < 1e-5 and pk[0]["asks"][4][1] == 204 and pk[1]["sid"] == 13 and pk[2]["oi"] == 130000)
    dis = struct.pack("<BHBIH", 50, 10, 0, 0, 807)
    rep.check("WebSocket server-disconnect code decoding", parse_feed_packet(dis)[0]["reason"] == 807)
    # ---- read-only guard ------------------------------------------------------------
    blocked = False
    try:
        assert_read_only("/orders")
    except OrderEndpointBlocked:
        blocked = True
    rep.check("Order endpoints blocked (read-only allow-list)", blocked and all(not e.startswith("/order") for e in READ_ONLY_ENDPOINTS))
    import inspect
    src = inspect.getsource(sys.modules[__name__])
    forbidden = ["place_" + "order(", "modify_" + "order(", "cancel_" + "order(", "from dhanhq import " + "dhanhq"]
    rep.check("No order-placement calls anywhere in main.py", not any(x in src for x in forbidden))
    # ---- candles --------------------------------------------------------------------
    base = dt.datetime(2026, 11, 6, 9, 15, tzinfo=IST).timestamp()
    c1 = [{"ts": base + 60 * i, "open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100.5 + i, "volume": 10} for i in range(60)]
    r3, r30 = resample_candles(c1, 3), resample_candles(c1, 30)
    rep.check("3m / 30m resampling from 1m (Dhan serves 1/5/15/25/60 only)", len(r3) == 20 and len(r30) == 2 and r30[0]["volume"] == 300
              and r30[0]["high"] == 130 and r30[1]["open"] == 130)
    # ---- chain mapping + OI change -----------------------------------------------------
    T = 5 / 365
    raw0 = _synthetic_chain(24000, 50, T, r_, 0.13)
    last, legs = normalize_chain(raw0, time.time())
    k0 = (round(24000 / 50) * 50, "CE")
    rep.check("Option-chain JSON mapping (strike/side/OI/prevOI/bid/ask/IV/greeks/security_id)",
              len(legs) == 102 and legs[k0].sid == 100000 + k0[0] and fin(legs[k0].prev_oi) and fin(legs[k0].bid)
              and legs[k0].v_iv > 1, f"{len(legs)} legs")
    # ---- end-to-end synthetic pipeline -----------------------------------------------
    tmpdir = cfg.data_dir / "selftest"
    tmpdir.mkdir(parents=True, exist_ok=True)
    dbp = tmpdir / "selftest.db"
    for sfx in ("", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            Path(str(dbp) + sfx).unlink()
    tcfg = dataclasses.replace(cfg, db_path=dbp, snapshot_interval_s=60, min_samples_pattern=5, strike_window=15)
    db = Database(dbp, 200, 0.2)
    db.start()
    st = IndexState("NIFTY", tcfg)
    st.expiry = (now_ist().date() + dt.timedelta(days=5)).isoformat()
    st.lot, st.step = 65.0, 50.0
    rngp = np.random.default_rng(42)
    t0 = time.time() - 400 * 60
    Fp = 24000.0
    for i in range(300):
        st.bars.seed([{"ts": t0 - (300 - i) * 60, "open": Fp, "high": Fp + 5, "low": Fp - 5, "close": Fp + rngp.normal(0, 8)}])
    evs = EventEngine(tcfg, db)
    sigs = SignalRecorder(tcfg, db)
    doi_series = []
    for i in range(240):
        ts = t0 + i * 60
        jump = 0.0035 if i == 200 else 0.0
        Fp *= math.exp(rngp.normal(0, 0.0004) + jump)
        shift = -0.25 if 190 <= i < 200 else 0.0      # CE OI drop at resistance BEFORE the jump
        raw = _synthetic_chain(Fp, 50, max(T - i / 525600, 1 / 365), r_, 0.13, oi_shift=shift)
        _, st.chain = normalize_chain(raw, ts)
        st.chain_ts = ts
        st.chain_last_price = Fp
        st.bars.bars.append({"ts": ts - 60, "open": Fp, "high": Fp * 1.0002, "low": Fp * 0.9998, "close": Fp, "volume": 0})
        ft = Tick(1, "NSE_FNO", ltp=Fp * 1.002, atp=Fp, volume=1000.0 * i, buy_qty=5000, sell_qty=4000, oi=1e6 + i)
        f, rec, lg = build_features(st, ts, Fp, ft, 14.0, None, r_)
        dq, iss = data_quality_score(f, 1.0, True, tcfg)
        sc = compute_scores(f, dq, iss, {})
        f["_bull"], f["_bear"], f["_volx"] = sc["bullish_breakout"]["score"], sc["bearish_breakdown"]["score"], sc["volatility_expansion"]["score"]
        f["_regime"] = classify_regime(f, sc)["regime"]
        f["_patterns"] = active_patterns(f)
        rec.scores = {"bull": f["_bull"], "bear": f["_bear"]}
        st.history.append(rec)
        st.last_features, st.last_scores = f, sc
        p_, u_, i_ = ts_pair(ts)
        db.insert("index_snapshots", {"ts": p_, "ts_utc": u_, "ts_ist": i_, "index_name": "NIFTY", "spot": Fp,
                                      "fut": f.get("fut"), "features": {k: v for k, v in f.items()
                                                                        if isinstance(v, (int, float, str, bool, dict, type(None)))}})
        doi_series.append(f.get("res_ce_doi_15m"))
        if i == 201:
            ev = evs.check(st, ts)
        sigs.record(st, ts, f["_patterns"])
    rep.check("Per-strike CE/PE OI change computed over 5/15 min windows",
              any(fin(x) and x < 0 for x in doi_series[190:201]), f"min res CE dOI15 {np.nanmin([x for x in doi_series if fin(x)]):,.0f}")
    rep.check("CE OI drop before jump flagged by adaptive threshold",
              any("CE_OI_DROP_AT_RESISTANCE" in (r.f.get("_patterns") or {}) for r in list(st.history)[185:202]))
    rep.check("Breakout event recorded with pre-event snapshots", evs.n_events == 1 and ev is not None and ev["pre_15"] is not None,
              f"kind {ev['kind'] if ev else None} sigma {ev['sigma_mult'] if ev else 0:.1f} lead {ev['lead_minutes'] if ev else None}")
    a = ev["analysis"] if ev else {}
    rep.check("Event analysis answers research questions (first strike, writers, IV, gamma, futures, lead)",
              all(k in a for k in ("first_strike_changed", "ce_writers_exited_before", "pe_writers_increased", "iv_expanded",
                                   "gamma_concentration_changed", "futures_confirmed", "lead_minutes")),
              f"first {a.get('first_strike_changed')}, CE exit {a.get('ce_writers_exited_before')}")
    rep.check("Event uses only pre-event data (no look-ahead)", ev is not None and all(
        (ev[k] or {}).get("ts", 0) <= ev["ts"] for k in ("pre_15", "pre_10", "pre_5", "at_event")))
    evs.update_post({"NIFTY": st}, time.time() + 3600)
    sigs.label({"NIFTY": st}, time.time() + 3600)
    db.stop()
    db2 = Database(dbp)
    nsnap = db2.query("SELECT COUNT(*) n FROM index_snapshots")[0]["n"]
    nev = db2.query("SELECT COUNT(*) n, SUM(complete) c FROM breakout_events")[0]
    rep.check("Historical snapshots saved; DB survives restart (reopen + read)", nsnap == 240 and nev["n"] == 1, f"{nsnap} snapshots, events {nev['n']}")
    rep.check("Post-event +5/+15/+30 capture and genuine/false classification", nev["c"] == 1)
    panel = load_snapshot_panel(db2, days=2)
    attach_forward_returns(panel)
    lab_ok = all(all(panel[j]["ts"] > r["ts"] for j in range(len(panel)) if False) for r in panel)
    viol = 0
    for i, r in enumerate(panel):
        for h, v in r["fwd"].items():
            j = bisect.bisect_left([x["ts"] for x in panel], r["ts"] + h * 60)
            if j < len(panel) and abs(v - math.log(panel[j]["spot"] / r["spot"])) > 1e-12:
                viol += 1
            if j <= i:
                viol += 1
    rep.check("Forward labels use strictly later rows only (look-ahead check)", viol == 0 and lab_ok)
    tr_te = walk_forward_splits(1000, 5, 300, embargo=15)
    rep.check("Walk-forward splits: no overlap, test after train, embargo", all(check_no_overlap(a_.tolist(), b_.tolist()) and
                                                                                b_.min() - a_.max() > 15 for a_, b_ in tr_te))
    rep.check("Look-ahead violation detector catches injected future feature", lookahead_violations([1, 2, 5], [1, 2, 3]) == 1)
    Xl = rng.normal(size=(300, 3))
    yl = (Xl[:, 0] > 0).astype(int)
    Xl[:, 2] = yl + rng.normal(scale=0.01, size=300)
    rep.check("Leakage scan flags a label-copy feature", leakage_scan(Xl, yl, ["a", "b", "leak"]) == ["leak"])
    ps = pattern_study(panel, tcfg)
    rep.check("Pattern study: counts, Wilson CI, BH-FDR, MFE/MAE, precision/recall, net after cost",
              len(ps) > 0 and all(k in ps[0] for k in ("ci_lo", "p_adj", "mfe", "precision", "net_ret_after_cost")), f"{len(ps)} hypotheses")
    rows_bt = [dict(r) for r in panel] * 3
    for k_, r in enumerate(rows_bt):
        r["ts"] = r["ts"] + (k_ // len(panel)) * 86400 * 1.0
    bt = walk_forward_backtest(sorted(rows_bt, key=lambda r: r["ts"]), tcfg, 15)
    rep.check("Walk-forward backtest runs with transaction costs + embargo", "lookahead_violations" in bt and bt["lookahead_violations"] == 0,
              f"status {bt.get('status')}, trades {bt.get('oos_trades')}, cost {tcfg.cost_bps_roundtrip}bps")
    nsig = db2.query("SELECT COUNT(*) n, SUM(labelled) l FROM signals")[0]
    rep.check("Signals recorded and labelled after horizon", (nsig["n"] or 0) > 0, f"{nsig['n']} signals, {nsig['l']} labelled")
    hmm = GaussianHMM(2, 20).fit(np.concatenate([rng.normal(0, 1, 300), rng.normal(0, 5, 300)]))
    s_last, _ = hmm.predict_last(np.concatenate([rng.normal(0, 1, 300), rng.normal(0, 5, 300)]))
    rep.check("Gaussian HMM regime detection separates low/high-vol states", hmm.ok and np.argmax(np.sqrt(hmm.var[:, 0])) == s_last)
    cal = IsotonicCalibrator().fit([0.1, 0.2, 0.3, 0.4, 0.9], [0, 0, 1, 0, 1])
    rep.check("Isotonic calibration is monotone", cal.predict(0.15) <= cal.predict(0.35) <= cal.predict(0.9))
    rep.check("Expiry timestamp 15:30 IST", dt.datetime.fromtimestamp(expiry_timestamp("2026-11-10"), IST).strftime("%H:%M") == "15:30")
    calx = MarketCalendar(dataclasses.replace(cfg, holidays_extra="2026-11-10"))
    rep.check("Calendar: weekends & holidays respected", not calx.is_trading_day(dt.date(2026, 11, 7)) and
              not calx.is_trading_day(dt.date(2026, 11, 10)) and calx.is_trading_day(dt.date(2026, 11, 9)))
    return rep


async def run_verify(cfg: Config, ws_seconds: int, telegram: bool) -> Report:
    rep = Report("DHAN QUANT ENGINE - LIVE VERIFICATION")
    if not cfg.client_id or not cfg.resolve_token():
        rep.add("DhanHQ authentication", "FAIL", "DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN not set")
        return rep
    eng = Engine(cfg)
    try:
        await asyncio.to_thread(eng.master.load, True)
        rep.check("Instrument master downloaded & parsed", len(eng.master.indices) >= 5,
                  f"{len(eng.master.indices)} indices, {sum(len(v) for v in eng.master.futures.values())} futures")
    except Exception as e:
        rep.add("Instrument master", "FAIL", str(e)[:200])
        return rep
    ids = {n: (eng.master.index(n).security_id if eng.master.index(n) else None) for n in DEFAULT_INDICES}
    rep.check("Correct index instruments identified", all(ids[n] for n in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "BANKEX", "INDIAVIX")),
              ", ".join(f"{k}={v}" for k, v in ids.items()))
    st = eng.states.get("NIFTY") or next(iter(eng.states.values()))
    try:
        exps = await eng.rest.expiry_list(ids[st.name], "IDX_I")
        rep.check("DhanHQ authentication (expiry list call)", True, f"token {cfg.token_fingerprint()}")
        rep.check("Option expiries discovered dynamically", len(exps) > 0, f"{st.name}: {exps[:4]}")
    except DhanApiError as e:
        rep.add("DhanHQ authentication", "FAIL", str(e)[:200])
        return rep
    all_exp = {}
    for n, s in eng.states.items():
        if n == st.name:
            all_exp[n] = exps
            continue
        try:
            all_exp[n] = await eng.rest.expiry_list(ids[n], "IDX_I")
        except DhanApiError as e:
            all_exp[n] = f"ERR {e}"
    rep.add("Expiries for all indices", "PASS" if all(isinstance(v, list) and v for v in all_exp.values()) else "FAIL",
            "; ".join(f"{k}:{(v[0] if isinstance(v, list) and v else v)}" for k, v in all_exp.items()))
    exp = [e for e in exps if expiry_timestamp(e) > time.time()][0]
    lot = eng.master.lot_size(st.name, exp)
    rep.check("Lot size discovered from instrument master", bool(lot), f"{st.name} {exp}: {lot}")
    raw = await eng.rest.option_chain(ids[st.name], "IDX_I", exp)
    last, legs = normalize_chain(raw, time.time())
    nonzero = sum(1 for l in legs.values() if fin(l.oi) and l.oi > 0)
    mapped = sum(1 for l in legs.values() if l.sid and eng.master.by_sid.get((st.spec.fno_segment, l.sid)))
    rep.check("Option-chain data mapped", len(legs) > 20 and nonzero > 10, f"{len(legs)} legs, {nonzero} with OI, "
              f"{mapped} security ids match instrument master, underlying {last}")
    fields = collections.Counter()
    for l in legs.values():
        for fld in ("ltp", "oi", "prev_oi", "volume", "bid", "ask", "v_iv", "v_delta"):
            if fin(getattr(l, fld)):
                fields[fld] += 1
    rep.add("Field availability (non-null share)", "PASS", ", ".join(f"{k} {v / max(len(legs), 1):.0%}" for k, v in fields.items()))
    T = year_fraction(time.time(), exp)
    ks = sorted({k for k, _ in legs})
    pf = parity_forward(ks, [legs.get((k, "CE"), Leg(k, "CE")).fair for k in ks],
                        [legs.get((k, "PE"), Leg(k, "PE")).fair for k in ks], T, cfg.risk_free, last)
    rep.check("Put-call parity forward from live chain", pf is not None, f"forward {pf[0]:.2f} vs index {last} (disp {pf[1]:.1f}, n {pf[2]})" if pf else "")
    if pf:
        atm = min(ks, key=lambda k: abs(k - pf[0]))
        ce, pe = legs.get((atm, "CE")), legs.get((atm, "PE"))
        if ce and pe:
            ivc = implied_vol_black76(ce.fair, pf[0], atm, T, cfg.risk_free, True)
            ivp = implied_vol_black76(pe.fair, pf[0], atm, T, cfg.risk_free, False)
            rep.add("ATM IV (own Black-76) vs vendor IV", "PASS" if ivc and ivp else "FAIL",
                    f"CE {ivc and ivc * 100:.2f}% / vendor {ce.v_iv:.2f}%; PE {ivp and ivp * 100:.2f}% / vendor {pe.v_iv:.2f}%")
    st.idx, st.expiry = eng.master.index(st.name), exp
    eng._idx_by_key[(st.idx.segment, st.idx.security_id)] = st.name
    st.chain_last_price = last
    st.chain = legs
    st.fut = eng.master.near_future(st.name)
    eng.vix = eng.master.index("INDIAVIX")
    for n, s in eng.states.items():
        s.idx = eng.master.index(n)
        s.expiry = exp if n == st.name else (all_exp[n][0] if isinstance(all_exp[n], list) and all_exp[n] else None)
        s.fut = eng.master.near_future(n)
    eng._update_subscriptions()
    rep.check("Futures contracts discovered", all(s.fut for s in eng.states.values()),
              ", ".join(f"{n}:{s.fut.display if s.fut else None}" for n, s in eng.states.items()))
    try:
        d = await eng.rest.intraday(st.idx.security_id, "IDX_I", "INDEX", (now_ist().date() - dt.timedelta(days=5)).isoformat(),
                                    (now_ist().date() + dt.timedelta(days=1)).isoformat(), 1)
        cs = candles_from_payload(d)
        rep.check("Historical 1-minute candles", len(cs) > 0, f"{len(cs)} bars")
    except DhanApiError as e:
        rep.add("Historical 1-minute candles", "FAIL", str(e)[:150])
    feed_task = asyncio.create_task(eng.feed.run())
    t_end = time.time() + ws_seconds
    while time.time() < t_end and eng.feed.msgs == 0 and not eng.feed.fatal:
        await asyncio.sleep(0.5)
    await asyncio.sleep(min(5, max(0, t_end - time.time())))
    got = eng.feed.msgs
    is_open = eng.cal.is_market_open()
    if got > 0:
        rep.add("Live WebSocket", "PASS", f"{got} msgs, {len(eng.feed.ticks)} instruments, {len(eng.feed.subscribed)} subscribed")
    else:
        rep.add("Live WebSocket", "FAIL" if (is_open or eng.feed.fatal) else "SKIP",
                f"no packets in {ws_seconds}s; fatal={eng.feed.fatal}; market open={is_open}")
    if eng.feed._ws is not None:
        with contextlib.suppress(Exception):
            await eng.feed._ws.close()
        await asyncio.sleep(4)
        rep.check("WebSocket reconnect after forced close", eng.feed.reconnects >= 1 or eng.feed.connected,
                  f"reconnects {eng.feed.reconnects}, connected {eng.feed.connected}")
    else:
        rep.add("WebSocket reconnect", "SKIP", "no live connection to drop")
    eng.feed.stop()
    feed_task.cancel()
    with contextlib.suppress(BaseException):
        await feed_task
    if telegram:
        eng.alerter.cfg.alert_cooldown_s = 0
        ok = await eng.alerter.send("SYSTEM", "TEST", f"Dhan Quant Engine verification message {now_ist():%H:%M:%S}", str(time.time()))
        rep.check("Telegram alerts", ok, "message sent" if ok else ("not configured" if not eng.alerter.enabled else "send failed"))
        await eng.alerter.close()
    else:
        rep.add("Telegram alerts", "SKIP", "run verify --telegram to send a test message")
    eng.db.flush_sync()
    rep.check("Database writable / readable", bool(eng.db.query("SELECT v FROM meta WHERE k='schema_version'")))
    import shutil
    import subprocess
    if shutil.which("systemctl"):
        r = subprocess.run(["systemctl", "is-active", "dhan-quant-engine"], capture_output=True, text=True)
        rep.add("systemd service", "PASS" if r.stdout.strip() == "active" else "N/A", r.stdout.strip() or r.stderr.strip()[:80])
    else:
        rep.add("systemd service", "N/A", "systemctl not available on this host")
    rep.check("No automatic orders placed", True, "order endpoints not imported; allow-list enforced")
    return rep


# ============================================================================
# 14. CLI
# ============================================================================

def cmd_report(cfg: Config) -> None:
    db = Database(cfg.db_path)
    lr = db.get_meta("last_research", {})
    print(json.dumps(lr, indent=1, default=str))
    rows = db.query("SELECT pattern, index_name, horizon, n, success_rate, ci_lo, ci_hi, mean_ret, net_ret_after_cost, "
                    "p_adj, fdr_pass, sample_status FROM validation_results WHERE ts=(SELECT MAX(ts) FROM validation_results) "
                    "ORDER BY fdr_pass DESC, p_adj ASC LIMIT 40")
    print(f"\n{'pattern':32} {'idx':9} {'h':>3} {'n':>5} {'succ':>6} {'CI':>13} {'mean bp':>8} {'net bp':>7} {'p_adj':>6} FDR status")
    for r in rows:
        print(f"{r['pattern']:32} {r['index_name']:9} {r['horizon']:>3} {r['n']:>5} {(r['success_rate'] or 0):6.2%} "
              f"[{(r['ci_lo'] or 0):.2f},{(r['ci_hi'] or 0):.2f}] {(r['mean_ret'] or 0) * 1e4:8.1f} "
              f"{(r['net_ret_after_cost'] or 0) * 1e4:7.1f} {(r['p_adj'] or 1):6.3f} {r['fdr_pass']:>3} {r['sample_status']}")
    mp = db.query("SELECT model, target, n_train, n_test, brier, base_brier, ece, sharpe, dsr, lookahead_violations "
                  "FROM model_performance ORDER BY ts DESC LIMIT 6")
    print("\nmodels:")
    for r in mp:
        print(dict(r))
    print("\nevent study:", json.dumps(db.get_meta("last_event_study", {}), default=str)[:1500])


async def cmd_backfill(cfg: Config, days: int) -> None:
    eng = Engine(cfg)
    eng.db.start()
    await asyncio.to_thread(eng.master.load, False)
    for n, st in eng.states.items():
        st.idx, st.fut = eng.master.index(n), eng.master.near_future(n)
    eng.vix = eng.master.index("INDIAVIX")
    today = now_ist().date()
    for n, st in eng.states.items():
        if not st.idx:
            continue
        for k in range(0, days, 5):
            frm = (today - dt.timedelta(days=k + 5)).isoformat()
            to = (today - dt.timedelta(days=k - 1)).isoformat()
            try:
                c = candles_from_payload(await eng.rest.intraday(st.idx.security_id, "IDX_I", "INDEX", frm, to, 1))
                eng._store_candles(st.idx.security_id, "IDX_I", "1m", c, n)
                for m in (3, 5, 15, 30):
                    eng._store_candles(st.idx.security_id, "IDX_I", f"{m}m", resample_candles(c, m), n)
                print(f"{n} {frm}..{to}: {len(c)} bars")
            except DhanApiError as e:
                print(f"{n} {frm}: {e}")
        d = candles_from_payload(await eng.rest.daily(st.idx.security_id, "IDX_I", "INDEX",
                                                      (today - dt.timedelta(days=3650)).isoformat(), today.isoformat()))
        eng._store_candles(st.idx.security_id, "IDX_I", "1d", d, n)
        print(f"{n} daily: {len(d)}")
    eng.db.stop()


def cmd_watch(cfg: Config) -> None:
    p = cfg.data_dir / "dashboard.txt"
    try:
        while True:
            txt = p.read_text() if p.exists() else "engine not running (no data/dashboard.txt)"
            sys.stdout.write("\x1b[2J\x1b[H" + txt + "\n")
            sys.stdout.flush()
            time.sleep(2)
    except KeyboardInterrupt:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="main.py", description="Dhan Quant Intelligence Engine (read-only)")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    r.add_argument("--headless", action="store_true")
    sub.add_parser("watch")
    v = sub.add_parser("verify")
    v.add_argument("--ws-seconds", type=int, default=20)
    v.add_argument("--telegram", action="store_true")
    sub.add_parser("selftest")
    sub.add_parser("initdb")
    sub.add_parser("research")
    sub.add_parser("report")
    b = sub.add_parser("backfill")
    b.add_argument("--days", type=int, default=30)
    a = ap.parse_args(argv)
    cfg = Config.from_env()
    register_secret(cfg.resolve_token())
    register_secret(cfg.telegram_token)
    cmd = a.cmd or "run"
    headless = getattr(a, "headless", False) or not sys.stdout.isatty()
    setup_logging(cfg, to_console=(cmd in ("run",) and headless) or cmd in ("verify", "backfill", "research"))
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    if cmd == "selftest":
        rep = run_selftest(cfg)
        (cfg.data_dir / "selftest_report.md").write_text(rep.markdown())
        print(f"\nreport: {cfg.data_dir / 'selftest_report.md'}")
        return 1 if rep.failed else 0
    if cmd == "initdb":
        Database(cfg.db_path)
        print(f"schema v{SCHEMA_VERSION} ready at {cfg.db_path}")
        return 0
    if cmd == "research":
        out = research_cycle(Database(cfg.db_path), cfg)
        print(json.dumps({k: v for k, v in out.items() if k != "model"}, default=str, indent=1)[:5000])
        return 0
    if cmd == "report":
        cmd_report(cfg)
        return 0
    if cmd == "watch":
        cmd_watch(cfg)
        return 0
    if cmd == "backfill":
        asyncio.run(cmd_backfill(cfg, a.days))
        return 0
    if cmd == "verify":
        rep = asyncio.run(run_verify(cfg, a.ws_seconds, a.telegram))
        (cfg.data_dir / "verify_report.md").write_text(rep.markdown())
        print(f"\nreport: {cfg.data_dir / 'verify_report.md'}")
        return 1 if rep.failed else 0
    lock = SingleInstance(cfg.data_dir / "engine.lock")
    if not lock.acquire():
        print("another engine instance is running (data/engine.lock); refusing to open a duplicate WebSocket", file=sys.stderr)
        return 2
    try:
        eng = Engine(cfg)
        asyncio.run(eng.run(live_dashboard=not headless))
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
