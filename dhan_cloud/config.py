"""Dhan Cloud configuration. Secrets are Dhan Cloud Variables ({{NAME}}), never literals."""

CLIENT_ID = "{{DHAN_CLIENT_ID}}"
ACCESS_TOKEN = "{{DHAN_ACCESS_TOKEN}}"
TELEGRAM_TOKEN = "{{TELEGRAM_BOT_TOKEN}}"
TELEGRAM_CHAT = "{{TELEGRAM_CHAT_ID}}"

BASE = "https://api.dhan.co/v2"
SCRIP_MASTER = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"

# Index security ids verified against the Dhan instrument master (IDX_I).
INDICES = {
    "NIFTY": {"sid": 13, "exch": "NSE", "fno": "NSE_FNO", "step": 50},
    "BANKNIFTY": {"sid": 25, "exch": "NSE", "fno": "NSE_FNO", "step": 100},
    "FINNIFTY": {"sid": 27, "exch": "NSE", "fno": "NSE_FNO", "step": 50},
    "SENSEX": {"sid": 51, "exch": "BSE", "fno": "BSE_FNO", "step": 100},
    "BANKEX": {"sid": 69, "exch": "BSE", "fno": "BSE_FNO", "step": 100},
}
VIX_SID = 21

PARAMS = {
    "adx_trend": 25.0, "er_trend": 0.3, "adx_mr": 20.0, "z_mr": 2.0,
    "rsi_lo": 40.0, "rsi_hi": 55.0, "squeeze_lookback": 20, "squeeze_memory": 5,
    "level_atr": 0.2, "stop_atr": 2.0, "risk_rupees": 5000.0, "max_spread_pct": 1.0,
    "pcr_low": 0.7, "pcr_high": 1.3,
}

RISK_FREE = 0.065
LOOP_SLEEP_S = 20
BAR_MINUTES = 5


def configured():
    return not CLIENT_ID.startswith("{{") and not ACCESS_TOKEN.startswith("{{")
