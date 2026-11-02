"""Feature declarations (blueprint §7): for each feature, its inputs, timeframe,
lookback, warm-up, null handling and publication time. The engine must produce
exactly this set. A test enforces it, so no undeclared feature can appear."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from doi.intelligence.feature_engine.config import FeatureConfig

Publication = Literal["bar_close", "snapshot_receipt", "clock"]


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    name: str
    family: str
    inputs: tuple[str, ...]
    lookback: str
    warmup: str
    null_policy: str
    publication: Publication
    unit: str = ""
    version: int = 1


def _bar(name, family, lookback, warmup, unit="", null="WARMUP until enough closed bars") -> FeatureSpec:
    return FeatureSpec(name, family, ("bars",), lookback, warmup, null, "bar_close", unit)


def _snap(
    name, family, lookback="latest snapshot", unit="", inputs=("snapshot",), null="NO_DATA without a usable snapshot"
) -> FeatureSpec:
    return FeatureSpec(
        name,
        family,
        inputs,
        lookback,
        "1 snapshot",
        null + "; STALE when older than max_snapshot_age_s",
        "snapshot_receipt",
        unit,
    )


def feature_specs(cfg: FeatureConfig) -> tuple[FeatureSpec, ...]:
    s: list[FeatureSpec] = []
    for n in cfg.ema_periods:
        s.append(_bar(f"ema_{n}", "trend", f"{n} bars", f"{n} bars", "index pts"))
    no_vol = "NULL when the bars carry no volume (index candles); supply futures bars"
    s += [
        FeatureSpec("vwap", "trend", ("bars",), "current session", "1 bar with volume", no_vol, "bar_close", "index pts"),
        FeatureSpec("vwap_distance_pct", "trend", ("bars",), "current session", "1 bar with volume", no_vol, "bar_close", "%"),
        _bar("adx", "trend", f"{2 * cfg.adx_period} bars", f"{2 * cfg.adx_period} bars"),
        _bar("plus_di", "trend", f"{cfg.adx_period} bars", f"{cfg.adx_period + 1} bars"),
        _bar("minus_di", "trend", f"{cfg.adx_period} bars", f"{cfg.adx_period + 1} bars"),
        _bar("market_structure", "trend", "all closed bars", "2 confirmed swing highs and lows", "HH_HL|LH_LL|MIXED"),
        _bar("rsi", "momentum", f"{cfg.rsi_period} bars", f"{cfg.rsi_period + 1} bars"),
        _bar("macd", "momentum", f"{cfg.macd[1]} bars", f"{cfg.macd[1]} bars", "index pts"),
        _bar(
            "macd_signal", "momentum", f"{cfg.macd[1] + cfg.macd[2]} bars", f"{cfg.macd[1] + cfg.macd[2] - 1} bars", "index pts"
        ),
        _bar("macd_hist", "momentum", f"{cfg.macd[1] + cfg.macd[2]} bars", f"{cfg.macd[1] + cfg.macd[2] - 1} bars", "index pts"),
        _bar("roc", "momentum", f"{cfg.roc_period} bars", f"{cfg.roc_period + 1} bars", "%"),
        _bar("atr", "volatility", f"{cfg.atr_period} bars", f"{cfg.atr_period} bars", "index pts"),
        _bar("atr_pct", "volatility", f"{cfg.atr_period} bars", f"{cfg.atr_period} bars", "%"),
        _bar("bb_width_pct", "volatility", f"{cfg.bb_period} bars", f"{cfg.bb_period} bars", "%"),
        _bar("realized_vol_pct", "volatility", f"{cfg.rv_window} returns", f"{cfg.rv_window + 1} bars", "% annualised"),
        FeatureSpec(
            "vix",
            "volatility",
            ("vix_bars",),
            "latest closed VIX bar",
            "1 bar",
            "NO_DATA without VIX bars",
            "bar_close",
            "index pts",
        ),
        FeatureSpec(
            "vix_change_pct",
            "volatility",
            ("vix_bars",),
            "previous session close",
            "2 sessions",
            "NO_DATA without a prior session",
            "bar_close",
            "%",
        ),
        FeatureSpec(
            "relative_volume",
            "volume",
            ("bars",),
            f"{cfg.rel_volume_window} bars",
            f"{cfg.rel_volume_window + 1} bars",
            no_vol,
            "bar_close",
            "x",
        ),
        FeatureSpec("volume_poc", "volume", ("bars",), "current session", "1 bar with volume", no_vol, "bar_close", "index pts"),
        FeatureSpec(
            "opening_range_high",
            "volume",
            ("bars",),
            f"first {cfg.opening_range_min} min",
            "window complete",
            "WARMUP until the window closes",
            "bar_close",
            "index pts",
        ),
        FeatureSpec(
            "opening_range_low",
            "volume",
            ("bars",),
            f"first {cfg.opening_range_min} min",
            "window complete",
            "WARMUP until the window closes",
            "bar_close",
            "index pts",
        ),
        FeatureSpec(
            "opening_range_state",
            "volume",
            ("bars",),
            f"first {cfg.opening_range_min} min",
            "window complete",
            "WARMUP until the window closes",
            "bar_close",
            "ABOVE|BELOW|INSIDE",
        ),
        _bar("range_expansion", "candle", "last bar + ATR", f"{cfg.atr_period} bars", "x ATR"),
        _bar("body_ratio", "candle", "last bar", "1 bar"),
        _bar("close_location", "candle", "last bar", "1 bar", "0..1"),
        _snap("forward", "options", unit="index pts"),
        _snap("forward_strikes_used", "options", unit="count"),
        _snap("forward_basis", "options", unit="index pts (forward − index LTP)"),
        _snap("atm_strike", "options", unit="strike"),
        _snap("strike_step", "options", unit="pts"),
        _snap("time_to_expiry_years", "options", unit="years ACT/365"),
        _snap("pcr_oi", "options", unit="ratio (ATM window)"),
        _snap("pcr_oi_all", "options", unit="ratio (all strikes)"),
        _snap("pcr_volume", "options", unit="ratio (ATM window)"),
    ]
    for w in cfg.pcr_change_windows_min:
        s.append(_snap(f"pcr_oi_chg_{w}m", "options", f"snapshot ~{w} min earlier", "ratio change"))
        s.append(_snap(f"pcr_vol_chg_{w}m", "options", f"snapshot ~{w} min earlier", "ratio change"))
    s += [
        _snap("pcr_oi_chg_session", "options", "first snapshot of session", "ratio change"),
        _snap("pcr_vol_chg_session", "options", "first snapshot of session", "ratio change"),
    ]
    for side in ("put", "call"):
        s += [
            _snap(f"{side}_wall", "options", unit="strike"),
            _snap(f"{side}_wall_share", "options", unit="share of side OI"),
            _snap(f"{side}_wall_distance_pct", "options", unit="% of forward"),
            _snap(f"{side}_wall_migration_steps", "options", f"snapshot ~{cfg.wall_lookback_min} min earlier", "strike steps"),
        ]
    s += [
        _snap("max_pain", "options", unit="strike"),
        _snap("max_pain_distance_pct", "options", unit="% of forward"),
        _snap("max_pain_change", "options", "first snapshot of session", "pts"),
        _snap("atm_iv_b76", "options", unit="vol pts (own Black-76)"),
        _snap("atm_iv_dhan", "options", unit="vol pts (vendor, quarantined)"),
        _snap("atm_iv_diff", "options", unit="vol pts (vendor − own)"),
        _snap("iv_skew", "options", unit="vol pts (OTM put − OTM call)"),
        _snap("atm_delta", "options"),
        _snap("atm_gamma", "options", unit="per pt²"),
        _snap("atm_theta", "options", unit="per calendar day"),
        _snap("atm_vega", "options", unit="per vol pt"),
        _snap("gamma_exposure_proxy", "options", unit="premium per 1% move (proxy)"),
        _snap("atm_spread_pct", "liquidity", unit="% (median, ATM window)"),
        _snap("atm_min_top_qty", "liquidity", unit="units"),
        _snap("atm_two_sided_share", "liquidity", unit="share"),
        _snap("snapshot_age_s", "liquidity", unit="s"),
    ]
    clk = ("clock", "calendar")
    for name, unit in (
        ("minutes_since_open", "min"),
        ("minutes_to_close", "min"),
        ("is_first_window", "bool"),
        ("is_last_window", "bool"),
        ("session_open", "bool"),
        ("is_expiry_day", "bool"),
        ("is_day_before_expiry", "bool"),
        ("dte_calendar", "days"),
        ("dte_trading", "trading days"),
    ):
        s.append(FeatureSpec(name, "time", clk, "as_of", "none", "NO_DATA without an expiry for expiry fields", "clock", unit))
    s += [
        FeatureSpec(
            "trend_regime",
            "regime",
            ("bars",),
            "ema_20/ema_50/adx",
            "max(50, 2×adx) bars",
            "WARMUP until inputs exist",
            "bar_close",
            "TREND_UP|TREND_DOWN|RANGE",
        ),
        FeatureSpec(
            "atr_pct_percentile",
            "regime",
            ("bars",),
            f"{cfg.regime.vol_percentile_window} ATR% values",
            "20 ATR% values",
            "WARMUP until 20 values",
            "bar_close",
            "percentile",
        ),
        FeatureSpec(
            "vol_regime",
            "regime",
            ("bars",),
            f"{cfg.regime.vol_percentile_window} ATR% values",
            "20 ATR% values",
            "WARMUP until 20 values",
            "bar_close",
            "HIGH|NORMAL|LOW",
        ),
    ]
    names = [x.name for x in s]
    if len(set(names)) != len(names):
        raise ValueError("duplicate feature names")
    return tuple(s)
