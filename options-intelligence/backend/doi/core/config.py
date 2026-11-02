"""Runtime settings from environment. Secrets are SecretStr and never logged."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, SecretStr

DEFAULT_STRATEGY_DIR = Path(__file__).resolve().parent.parent / "intelligence" / "strategy_configs"


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    dhan_base_url: str = "https://api.dhan.co/v2"
    dhan_client_id: str | None = None
    dhan_access_token: SecretStr | None = None
    dhan_token_file: Path | None = None
    instrument_master_url: str = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
    http_timeout_s: float = 10.0
    chain_cooldown_s: float = 3.0
    chain_max_concurrency: int = 4
    historical_cooldown_s: float = 3.0
    historical_max_concurrency: int = 1
    database_url: SecretStr | None = None
    redis_url: str | None = None
    strategy_config_dir: Path = DEFAULT_STRATEGY_DIR
    paper_starting_capital: float = 500000.0
    paper_ledger_path: Path | None = None
    paper_feed_interval_s: float = 3.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        fields = {
            "dhan_base_url": "DHAN_BASE_URL",
            "dhan_client_id": "DHAN_CLIENT_ID",
            "dhan_access_token": "DHAN_ACCESS_TOKEN",
            "dhan_token_file": "DHAN_TOKEN_FILE",
            "instrument_master_url": "DHAN_INSTRUMENT_MASTER_URL",
            "http_timeout_s": "HTTP_TIMEOUT_S",
            "chain_cooldown_s": "CHAIN_COOLDOWN_S",
            "chain_max_concurrency": "CHAIN_MAX_CONCURRENCY",
            "historical_cooldown_s": "HISTORICAL_COOLDOWN_S",
            "historical_max_concurrency": "HISTORICAL_MAX_CONCURRENCY",
            "database_url": "DATABASE_URL",
            "redis_url": "REDIS_URL",
            "strategy_config_dir": "STRATEGY_CONFIG_DIR",
            "paper_starting_capital": "PAPER_STARTING_CAPITAL",
            "paper_ledger_path": "PAPER_LEDGER_PATH",
            "paper_feed_interval_s": "PAPER_FEED_INTERVAL_S",
        }
        data = {f: env[k] for f, k in fields.items() if env.get(k)}
        return cls(**data)
