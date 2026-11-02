"""Versioned per-index configuration. One shared engine, five config files;
no index silently inherits another's parameters (extra keys are rejected and
every file must declare its own values)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from doi.core.errors import ConfigError

UNIVERSE = ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "BANKEX")
WEIGHT_GROUPS = ("trend", "momentum", "pcr_oi", "oi_walls", "volatility_greeks", "liquidity", "market_time")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Freshness(_Strict):
    chain_max_age_s: float = Field(gt=0)
    underlying_max_age_s: float = Field(gt=0)


class Liquidity(_Strict):
    max_spread_pct: float = Field(gt=0)
    min_oi: int = Field(ge=0)
    min_volume: int = Field(ge=0)
    max_quote_age_s: float = Field(gt=0)


class Historical(_Strict):
    status: Literal["blocked", "pilot", "approved"]
    expiry_flags: tuple[Literal["WEEK", "MONTH"], ...]
    max_atm_offset: int = Field(ge=0, le=10)
    max_atm_offset_near_expiry: int = Field(ge=0, le=10)


class Thresholds(_Strict):
    """None = not calibrated. The signal engine must return WAIT, not guess."""

    bull_min: float | None
    bear_min: float | None
    margin: float | None


class IndexConfig(_Strict):
    key: Literal["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "BANKEX"]
    config_version: str
    exchange_family: Literal["NSE", "BSE"]
    master_symbol: str
    live_enabled: bool
    activation_gate: str
    freshness: Freshness
    liquidity: Liquidity
    historical: Historical
    weights: dict[str, float]
    thresholds: Thresholds

    @field_validator("weights")
    @classmethod
    def _weights(cls, w: dict[str, float]) -> dict[str, float]:
        if set(w) != set(WEIGHT_GROUPS):
            raise ValueError(f"weights must be exactly {WEIGHT_GROUPS}")
        if any(v < 0 for v in w.values()) or abs(sum(w.values()) - 100) > 1e-9:
            raise ValueError("weights must be non-negative and total 100")
        return w


def load_index_config(path: Path) -> IndexConfig:
    try:
        cfg = IndexConfig.model_validate(yaml.safe_load(path.read_text()))
    except (ValidationError, yaml.YAMLError) as e:
        raise ConfigError(f"{path.name}: {e}") from None
    if path.stem != cfg.key.lower():
        raise ConfigError(f"{path.name}: file name does not match key {cfg.key}")
    return cfg


def load_index_configs(directory: Path) -> dict[str, IndexConfig]:
    cfgs = {c.key: c for c in (load_index_config(p) for p in sorted(directory.glob("*.yaml")))}
    missing = set(UNIVERSE) - set(cfgs)
    if missing:
        raise ConfigError(f"missing index configs: {sorted(missing)}")
    return cfgs
