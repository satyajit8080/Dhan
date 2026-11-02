"""Versioned feature-engine configuration. Every FeatureVector records the
config version plus a hash of its full content, so a silent parameter change can't
pass for the same features."""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from doi.core.canonical import canonical


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RegimeConfig(_Strict):
    adx_trend_min: float = Field(20.0, gt=0)
    vol_percentile_window: int = Field(100, ge=20)
    vol_high_pct: float = Field(80.0, gt=50, le=100)
    vol_low_pct: float = Field(20.0, ge=0, lt=50)


class FeatureConfig(_Strict):
    version: str = "fc-1"
    ema_periods: tuple[int, ...] = (9, 20, 50, 200)
    rsi_period: int = Field(14, ge=2)
    macd: tuple[int, int, int] = (12, 26, 9)
    roc_period: int = Field(10, ge=1)
    atr_period: int = Field(14, ge=2)
    adx_period: int = Field(14, ge=2)
    bb_period: int = Field(20, ge=2)
    bb_k: float = Field(2.0, gt=0)
    rv_window: int = Field(20, ge=2)
    rel_volume_window: int = Field(20, ge=2)
    volume_bin_steps: int = Field(1, ge=1)  # POC bin = strike step * this
    opening_range_min: int = Field(15, ge=1)
    swing_k: int = Field(2, ge=1)
    pcr_strike_window: int = Field(10, ge=1)  # ATM ± n strikes
    pcr_change_windows_min: tuple[int, ...] = (5, 15, 60)
    wall_lookback_min: int = Field(15, ge=1)
    skew_strikes: int = Field(2, ge=1)  # OTM distance (strike steps) for skew
    liquidity_strikes: int = Field(2, ge=0)
    parity_band_pct: float = Field(1.5, gt=0)
    rate: float = Field(0.065, ge=0, le=0.25)  # research parameter for discounting
    oi_unit: Literal["units", "contracts"] = "units"  # REQUIRES VERIFICATION against Dhan chain docs
    max_snapshot_age_s: float = Field(15.0, gt=0)
    first_window_min: int = Field(30, ge=1)
    last_window_min: int = Field(90, ge=1)
    trading_days_per_year: int = 252
    session_minutes: int = 375
    regime: RegimeConfig = RegimeConfig()

    @model_validator(mode="after")
    def _check(self) -> FeatureConfig:
        if len(set(self.ema_periods)) != len(self.ema_periods) or min(self.ema_periods) < 2:
            raise ValueError("ema_periods must be distinct and >= 2")
        if 20 not in self.ema_periods or 50 not in self.ema_periods:
            raise ValueError("ema_periods must include 20 and 50 (used by the trend regime)")
        f, s, sig = self.macd
        if not 1 <= f < s or sig < 1:
            raise ValueError("macd must be (fast < slow, signal >= 1)")
        return self

    @property
    def hash(self) -> str:
        return hashlib.sha256(canonical(self.model_dump(mode="json")).encode()).hexdigest()
