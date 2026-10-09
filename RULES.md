# Bull50 — Rules

Every rule the SENSEX scanner follows, in one place. Where code and this file
disagree, fix the code.

## 1. Safety (non-negotiable)

- **Read-only.** The server never places, modifies or cancels orders. No
  positions, funds or margin are touched.
- **Paper-trading module stays deleted.** It was built and removed on request
  (23 Sep 2026). Do not re-add it without an explicit ask.
- **Never echo the Dhan token.** Show the fingerprint (last 4) only. Tokens are
  held in memory or read from `DHAN_TOKEN_FILE`, never written to disk by the
  server, and redacted from logs.
- **Never commit secrets.** `.env`, `*.token` are git-ignored. Only
  `.env.example` ships.
- **Never type the user's credentials, PIN or OTP.** The user logs in themselves.
- **Dhan Cloud:** credentials go in `{{VAR}}` Variables, never in code.

## 2. Pricing (why the numbers are trustworthy)

- **The index LTP is never spot.** It can sit 300+ points below the true
  forward. Spot = forward recovered from put-call parity:
  `F_K = K + (C − P) / DF`, median across strikes within a 1.5% ATM band.
- **Leg prices** use the bid/ask mid when the quote is tight (spread ≤ 5% of
  mid), else LTP (`fairPrice`, 30 Sep fix). ATM hint = strike where |C − P| is
  smallest.
- **Black-76 on the forward** for IV and all Greeks. Vendor IV/Greeks are
  quarantined (about 50% of legs are 0, missing or above 60%).
- Greek units: delta dV/dF discounted, gamma per pt², vega per 1 IV pt,
  theta per calendar day, rho −T·V per 1%. T = ACT/365 to 15:30 IST expiry.

## 3. Gate (publish nothing if any fails)

| Check | Threshold |
|---|---|
| Per-strike parity spread | ≤ 40 pts |
| Annualised carry, forward vs listed future | −5% to +15% |
| Same-expiry future divergence | ≤ 75 pts |
| Chain / futures fetch skew | ≤ 3000 ms |
| Option quote age | stale flag after 15 s |

The futures registry must hold the next contract before the current one
expires (**add DEC before 26 Nov 2026**).

## 4. Levels

- Source: today's 5-minute **SENSEX index** candles (INDmoney `INDI00050` or
  Dhan), passed to `compute_levels` with spot = index LTP.
- Clustering + touch counting + spike rejection. A non-structural level needs
  **≥ 2 touches**; confirmation buffer ±5, rounded to 5.
- CE trigger = `breakoutAbove`; PE trigger = `breakdownBelow`.

## 5. The `refresh` command

Output is **one table, nothing else**:

| Strike | Type | LTP | Breakout Above |
|---|---|---|---|

- ATM±2 strikes, CE and PE (10 rows).
- **Breakout Above = the option's projected premium at the trigger level**, not
  an index level. Full Black-76 reprice at the trigger with server IV and theta
  for expected time-to-trigger (distance / ATR × bar length).
- Every refresh is a brand-new live scan. Nothing cached.
- Blank table `| — | — | — | — |` when: gate blocked · no token ·
  candle/snapshot skew > 350 s · no-trade window.
- **No-trade windows (IST):** 09:15–09:25, 11:30–13:00, after 14:50.
- `WEAK_LEVEL` when the nearest level has fewer than 2 touches.

## 6. Scalping rules (v3)

- Scope: SENSEX nearest weekly expiry (next weekly on expiry day), CE and PE.
- Breakout scalp: 1-min close through a level, next bar holds, volume > 10-bar
  average → enter on retest / second bar.
- Rejection scalp: 2 consecutive 1-min wicks rejecting a level → opposite side.
- No entry within 3 pts of a level.
- Exits: target next level or +1.5× stop; stop on 1-min close back through the
  level; time stop 10 min.
- Filters: spread < 0.5% of premium; liquidity grade A/B at size (judge on
  round-trip cost, not quoted spread); theta over 10 min < 1% of premium;
  IV ≤ chain ATM IV + 1 pt.
- Risk: max loss/trade 1% of available margin; stop after 3 losing scalps/day.
- **Direction thresholds are not configured.** Without them the server returns
  NO SIGNAL — never invent a CE/PE verdict.

## 7. Automation roadmap (not built)

1. Paper-trade 2–3 weeks with real timestamps and slippage.
2. Dhan Sandbox (`https://sandbox.dhan.co/v2/`).
3. 1-lot live with trade cap, daily loss cap and a kill switch.

Dhan Cloud constraints: Python 3.11 only, 1 vCPU / 3 GB, continuous loop with
sleep, no `os.getenv`/`os.path`/file writes/subprocess/Excel libs/port binding,
pinned dependencies, no persistent storage, AI code scanner blocks on any flag,
split files over ~200 lines. Outbound HTTP allowed (Telegram alerts possible).

## 8. Facts

- SENSEX: securityId 51 / IDX_I; options BSE_FNO; lot size 20.
- Futures: SEP 844615 (exp 24 Sep), OCT 864571 (exp 29 Oct), NOV 1100929 (exp 26 Nov).
- NIFTY / BANKNIFTY security ids are **unverified**.
- Rate limits: option chain 1 unique / 3 s, quotes 1 / s.
