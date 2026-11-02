# Next steps (resume here)

Last commit: Grok client aligned with official xAI docs (1def955). 257 tests pass.

## Can do now (no credentials needed, ~1-2 h)
1. Recalculate the per-meeting cost estimate with the verified Grok prices, and update docs/phase7-deployment.md and docs/phase8-validation.md.
2. Check the Dhan docs (dhanhq.co, now reachable): history depth, intraday look-back, WebSocket behaviour.
3. Fetch the official NSE circular NSE/FA/73061 and the 2026 holiday list. NSE blocks plain curl, so retry with a browser user-agent, then mark them verified.
4. Record the live findings in docs/phase8-validation.md: real master index ids (13/25/27/51/69), lot sizes and verified xAI prices.

## Needs credentials
5. Run `XAI_API_KEY=... python scripts/grok_live_check.py` (capped at ₹20).
6. Run `DHAN_CLIENT_ID=... DHAN_ACCESS_TOKEN=... python scripts/smoke_live.py` during market hours.

## Needs time and infrastructure
7. 10 supervised paper-trading sessions (runbook in docs/phase8-validation.md).
8. Docker image build on a machine with Docker.
