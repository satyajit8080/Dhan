# Third-party notices

No third-party source code has been copied or adapted into this project yet.

| Component | Use | Licence | Version / commit |
|---|---|---|---|
| FastAPI, Starlette | Runtime dependency | MIT / BSD-3 | pinned in `backend/pyproject.toml` |
| httpx | Runtime dependency | BSD-3 | pinned |
| pydantic | Runtime dependency | MIT | pinned |
| PyYAML | Runtime dependency | MIT | pinned |
| psycopg 3 | PostgreSQL driver | LGPL-3.0 (dynamic use, unmodified) | pinned |
| websockets | Live Market Feed client | BSD-3 | pinned |
| dhan-oss/DhanHQ-py `marketfeed.py` | Reference for the feed's binary packet layout only. No code copied | MIT | commit 8c6583e |
| DhanHQ v2 REST API | Endpoint contract (option chain, expiry list, charts, rolling option) | Dhan API terms | docs checked 02 Nov 2026 |

Before vendoring from marketcalls/openbull, marketcalls/openalgo (AGPL-3.0), bhawuk-arora/Backtesting-NSE
or MrChartist/india-s-best-option-hub: verify the canonical repo, licence file and commit SHA, then
record them here (blueprint section 3).
