"""Runtime settings from environment. Secrets are SecretStr and never logged."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, SecretStr

DEFAULT_HOLIDAYS = Path(__file__).resolve().parent / "holidays.yaml"
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
    dhan_feed_enabled: bool = False
    holidays_file: Path | None = DEFAULT_HOLIDAYS
    # Phase 7 AI research (off unless explicitly enabled AND a key is configured)
    ai_enabled: bool = False
    xai_api_key: SecretStr | None = None
    xai_base_url: str = "https://api.x.ai/v1"
    llm_fast_model: str = "grok-4.20-0309-non-reasoning"
    llm_reasoning_model: str = "grok-4.20-0309-reasoning"
    llm_monthly_limit_inr: float = 3000.0
    llm_safety_buffer_inr: float = 150.0
    llm_fx_inr_per_usd: float = 90.0
    llm_tax_multiplier: float = 1.18
    secrets_dir: Path = Path("./secrets")  # keys saved from the Settings page (0600 files)

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
            "dhan_feed_enabled": "DHAN_FEED_ENABLED",
            "holidays_file": "HOLIDAYS_FILE",
            "ai_enabled": "AI_ENABLED",
            "xai_api_key": "XAI_API_KEY",
            "xai_base_url": "XAI_BASE_URL",
            "llm_fast_model": "LLM_FAST_MODEL",
            "llm_reasoning_model": "LLM_REASONING_MODEL",
            "llm_monthly_limit_inr": "LLM_MONTHLY_LIMIT_INR",
            "llm_safety_buffer_inr": "LLM_SAFETY_BUFFER_INR",
            "llm_fx_inr_per_usd": "LLM_FX_INR_PER_USD",
            "llm_tax_multiplier": "LLM_TAX_MULTIPLIER",
            "secrets_dir": "SECRETS_DIR",
        }
        data = {f: env[k] for f, k in fields.items() if env.get(k)}
        # Keys saved from the Settings page; environment variables take precedence.
        from doi.core import setup_store
        from doi.core.secret_store import SecretDirError, SecretStore

        store = SecretStore(Path(data.get("secrets_dir", "./secrets")))
        try:
            for field_name, secret in (("dhan_client_id", "dhan_client_id"), ("xai_api_key", "xai_api_key")):
                if field_name not in data and (v := store.read(secret)):
                    data[field_name] = v
            for k, v in setup_store.load(store.dir).items():
                if k in ("ai_enabled", "dhan_feed_enabled") and k not in data:
                    data[k] = v
        except SecretDirError as e:  # start anyway; the Settings page shows the fix
            import logging

            logging.getLogger(__name__).error("%s", e)
        if "dhan_access_token" not in data and "dhan_token_file" not in data:
            data["dhan_token_file"] = str(store.path("dhan_access_token"))
        return cls(**data)
