"""Static settings for the Bull50 historical stock-options collector."""
from __future__ import annotations

from datetime import time, timedelta, timezone
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_DIR = PACKAGE_DIR.parent
DEFAULT_DATA_DIR = REPO_DIR / "data" / "options_history"
DEFAULT_SYMBOLS_FILE = PACKAGE_DIR / "symbols_60.txt"

BASE_URL = "https://api.dhan.co/v2"
ROLLING_PATH = "/charts/rollingoption"
SCRIP_MASTER_URLS = (
    "https://images.dhan.co/api-data/api-scrip-master-detailed.csv",
    "https://images.dhan.co/api-data/api-scrip-master.csv",
)

IST = timezone(timedelta(hours=5, minutes=30))

# Rolling-option request shape (stock options, monthly).
EXCHANGE_SEGMENT = "NSE_FNO"
INSTRUMENT = "OPTSTK"
EXPIRY_FLAG = "MONTH"
INTERVAL = 1                                  # minutes
OFFSETS = (-3, -2, -1, 0, 1, 2, 3)             # Dhan max for stock options is ATM+/-3
OPTION_TYPES = (("CALL", "ce"), ("PUT", "pe"))
REQUIRED_DATA = ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"]
WINDOW_DAYS = 30                              # Dhan: at most 30 days per call
MAX_YEARS = 5                                 # Dhan: up to 5 years back

# Observations to extract (IST minute bars).
SNAPSHOTS = (time(9, 30), time(15, 20))
DEFAULT_TOLERANCE_MIN = 2                     # nearest bar within +/- N min if exact is missing

# Rate limiting. Dhan Data APIs: ~5 req/s, ~100k/day.
DEFAULT_RPS = 4.0
DEFAULT_DAILY_QUOTA = 95_000
MAX_RETRIES = 6

# NSE monthly stock-option expiry: last Thursday, last Tuesday from this date on.
TUESDAY_EXPIRY_FROM = (2025, 9, 1)

# Expiry mapping check: on expiry day at 15:20 the ATM straddle should be almost
# all intrinsic. If it exceeds this share of spot, the expiry mapping is suspect.
EXPIRY_STRADDLE_MAX_PCT = 1.5

CSV_COLUMNS = [
    "date", "time", "symbol", "expiry", "strike", "ce_oi", "pe_oi", "ce_ltp", "pe_ltp",
    "spot", "ce_iv", "pe_iv", "ce_volume", "pe_volume", "lot_size",
]
