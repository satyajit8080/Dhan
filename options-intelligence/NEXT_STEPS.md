# Next steps (resume here)

Last commit: Grok client aligned with official xAI docs (1def955). 257 tests pass.

## Done 2026-11-02
- Per-meeting cost recalculated with verified Grok prices (≈ ₹4.2/meeting, ≈ ₹280/month) — docs updated.
- Dhan docs checked: intraday 5 years look-back, ≤ 90 days per request (now enforced); expired options ≤ 30 days per call, ATM ±10; WebSocket ≤ 5 connections × 5,000 instruments, ≤ 100 per message, server ping every 10 s; option chain 1 unique request / 3 s. All match the code.
- Live findings recorded in docs/phase8-validation.md.
- Backend display analytics added: option-chain `analytics` (Black-76 IV/Greeks, PCR, Max Pain, walls), `session` in system status, `/api/v1/ai/usage`, `/api/v1/paper/equity-curve`.
- Terminal UI rebuilt (`/dashboard`): 15-section institutional shell, global kill switch + ₹3,000 budget meter + hard-stop banner on every page, loading/empty/error/disconnected states; read-only endpoints added (`/ai/predictions`, `/research/strategies|experiments|option-backtests`, `/paper/analytics`), committee summary in meeting detail. Regime/VIX via `/api/v1/market/features` (deterministic feature engine on recorded data).

## Still open (blocked)
3. Official NSE circular NSE/FA/73061 and 2026 holiday list: NSE site blocks automated fetches (bot protection); download manually and confirm.

## Needs credentials
5. Run `XAI_API_KEY=... python scripts/grok_live_check.py` (capped at ₹20).
6. Run `DHAN_CLIENT_ID=... DHAN_ACCESS_TOKEN=... python scripts/smoke_live.py` during market hours.

## Needs time and infrastructure
7. 10 supervised paper-trading sessions (runbook in docs/phase8-validation.md).
8. Docker image build on a machine with Docker.
