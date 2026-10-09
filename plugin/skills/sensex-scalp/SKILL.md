---
name: sensex-scalp
description: >
  SENSEX options scalping signals against live Dhan data. Use whenever the user
  asks about SENSEX (or NIFTY/BANKNIFTY) option strikes, a signal, a trade, the
  chain, IV, Greeks, liquidity, or says "refresh". Enforces STRICT SIGNAL OUTPUT
  MODE: the reply contains only the signal, never the analysis.
---

# SENSEX scalping — STRICT SIGNAL OUTPUT MODE

## 0. `REFRESH` — HIGHEST PRIORITY PRESENTATION RULE

When the user types **refresh** (any casing), run a complete fresh live scan —
new `get_market_snapshot`, fresh candles, nothing reused — and reply with
**ONE TABLE AND NOTHING ELSE**:

```
| Strike | Type | LTP | Breakout Above |
|---|---|---|---|
| 72500 | CE | 427.50 | 445 |
| 72500 | PE | 181.35 | 211 |
| ... ATM-2 .. ATM+2, CE and PE for every strike (10 rows) ... |
```

- **Strike / Type** — ATM±2 strikes (ATM from the parity forward), both CE and PE.
- **LTP** — the live option price (ask when a tight quote exists).
- **Breakout Above** — the OPTION'S projected premium at the trigger level,
  NOT an index level. CE rows reprice at `breakoutAbove`, PE rows at
  `breakdownBelow`, via Black-76 on the parity forward with the server's own IV
  and theta for the expected time to trigger (`engine/refresh_table.py`).

Pipeline: `get_market_snapshot` (SENSEX, band 300) → today's 5-minute SENSEX
index candles (INDmoney INDI00050, or Dhan) → `compute_levels` with spot = index
LTP → `refresh_table.build_refresh_table` → table.

Blank table (`| — | — | — | — |`) and nothing else when: the gate blocks, no
token, candles/snapshot more than 350 s apart, or inside a no-trade window
(09:15–09:25, 11:30–13:00, after 14:50 IST). The user asks "why" separately.

### The candle endpoint being down is NOT a reason to fail

`get_analysis` derives levels from three sources in order, and reports which
one it used in `levelSource`:

1. `candles` — live intraday candles from Dhan
2. `rolling_snapshots` — synthetic bars built from the rolling history of every
   successful scan (index LTP, forward, futures, day high/low)
3. `reference_only` — exchange day high/low from the futures quote

### Tier 1b — an external candle source

If a Dhan candle call fails, **check whether another connector in this session
can supply SENSEX OHLC**, before falling back to tier 2. An INDmoney-style
connector exposing an Indian-market OHLC tool is the usual one:

1. resolve SENSEX to its key with that connector's lookup tool
2. request `5minute` (or `1minute`) candles with a `1d` lookback
3. pass the returned bars straight into **`compute_levels`**, which runs the
   identical clustering / touch-counting / spike-rejection engine

`compute_levels` accepts `datetime_ist` strings or epoch timestamps, so most
connectors' output can be forwarded without reshaping.

Two cautions when doing this:
- The external series must be the **SENSEX index itself**, not an ETF, a
  Sensex-tracking mutual fund, or Sensex-50. Check the resolved name.
- Levels derived from a different vendor's bars may differ slightly from Dhan's.
  That is a data difference, not an error; it is why `source` is reported.

### First, rule out the boring cause

`DH-901`, Data API `807` (token expired) / `808` / `809` / `810`, HTTP
401/403 or the plugin reporting `present: false` are **authentication
failures, not an outage**. (`DH-906` is an order error, not a token error.)
`806` / `DH-902` mean the Data API plan is not active — a new token will not
fix that. Each chat
session spawns its own server process, so a token set in one chat is NOT set in
another. Before declaring candles "down", check `dhan_token_status` — if the
token is missing or expired, the fix is `/dhan-token`, not a fallback.

Tiers 2 and 3 need no candle endpoint at all. A dead `/v2/charts/intraday`
therefore does NOT justify the empty table — check `levels.breakoutAbove` and
`levels.breakdownBelow` regardless of `candleErrors`.

**Never print a candle error during REFRESH.** Not as a note, not as a footer,
not as a parenthetical. `levelFailure` and `candles.errors` are diagnostics for
the "why" question only.

Every scan you run feeds the rolling history, so consecutive REFRESH calls get
progressively better levels even while candles stay down.

If the level genuinely could not be derived from ANY of the three tiers — or
the gate blocked, or there is no token — reply with EXACTLY this and nothing
else:

```
| Strike Price | Type | Breakout Above |
|---:|:---:|:---:|
| — | — | — |
```

No explanation with it. The user can ask "why" as a separate question, and
only then do you explain.

## 1. THE OUTPUT CONTRACT — for every request that is NOT `REFRESH`

Do all the analysis internally. Report only the block below.

```
SENSEX: 74756
ATM: 74800

BREAKOUT ABOVE: 74800
BREAKDOWN BELOW: 74690

CE/PE: CE 74800
ENTRY: 125
TARGET: 168
STOP LOSS: 96

REASON:
- Price-action breakout above confirmed resistance (3 touches)
- OI confirmation: put writing at 74700, call unwinding at 74800
- Volume confirmation: 1.8x the 20-bar average
- IV 11.2%, stable
- Delta 0.52, gamma 0.0004
- Liquidity grade A, round-trip 0.41% at 5 lots
```

Or, when nothing qualifies:

```
NO SIGNAL
```

### Hard rules
1. **The breakout level is mandatory.** Never omit it, never say
   "breakout unavailable" without having called `get_analysis`, which runs the
   candle endpoint and retries once on failure.
2. The trigger is an **underlying SENSEX index level**, never an option premium.
   It comes from `levels.breakoutAbove` / `levels.breakdownBelow`.
3. **Never invent a level, price, OI, volume, Greek or IV.** If the candle
   endpoint fails, report the actual API error from `levelFailure` — the exact
   text, including the endpoint name. Do not paper over it.
4. Emit a signal ONLY when every gate in section 3 passes.
5. If no valid setup exists — exactly `NO SIGNAL`.
6. One signal only, unless several are explicitly requested.
7. ENTRY is the live option LTP. TARGET and STOP are the delta/gamma
   projections in `tradePlans`, which are estimates from the Greeks and not
   quotes — never present them as quoted prices.
8. REASON lists the confirmations that actually fired. Never pad it with
   conditions that did not.

### Never display
Chain tables · raw PCR numbers · per-strike OI dumps · max pain tables ·
score breakdowns · internal calculations · tool names · "let me check"
narration. The REASON block is a summary, not a data dump.

### The escape hatch
If the user explicitly asks "why", "explain", "show working" or "debug", give
the full reasoning and the gate findings. Volunteering it is a violation;
answering a direct question is not.

## 2. THE `refresh` COMMAND

When the user says **refresh** (alone or with SENSEX), immediately run a
completely fresh `get_market_snapshot` — never reuse the previous result, never
serve from anything cached in the conversation.

Then output the new result in the exact format above. Do not say what changed.
Do not say you refreshed. Do not repeat the previous signal unless it
independently passes every gate again on the new data.

So `refresh` returns exactly one of: a CE line, a PE line, or `NO SIGNAL`.

## 3. VALIDATION GATES — all must pass, or `NO SIGNAL`

Run `get_market_snapshot` with `include_depth: true` and the user's configured
size. Then, internally:

**Gate A — data integrity.** `published` must be `true`. If the server blocked,
that is an automatic `NO SIGNAL`. Never work around a block, never fall back to
the index LTP, never assemble the pieces manually to dodge the gate.

**Gate B — pricing sanity.** `iv_pct` must not be null on the candidate leg. A
null IV means the quote was outside no-arbitrage bounds — the price is untrusted,
so the signal is untrusted.

**Gate C — liquidity.** The candidate strike must grade **A or B** at the user's
size. C or F is `NO SIGNAL` — a signal you cannot fill at your size is not a
signal. Judge on `roundtrip_pct`, never on the quoted spread.

**Gate D — freshness.** If the snapshot carries a staleness warning on the
candidate leg, or the market is closed, `NO SIGNAL`.

**Gate E — a real trigger level exists.** `levels.breakoutAbove` (for CE) or
`levels.breakdownBelow` (for PE) must be non-null. If `levelFailure` is set,
report that error text verbatim instead of a signal.

**Gate F — direction.** See section 4.

## 4. DIRECTION — THE ONE RULE STILL NOT CONFIGURED

Everything needed to decide is now computed and available in `get_analysis`:

- `levels` — confirmed resistance/support with touch counts, from 1-minute
  price action; `levels5m` derived from the same bars for cross-checking
- `priceStructure` — swings, higher-highs/lower-lows, consolidation
- `candleIndicators` — EMA, ADX, +DI/−DI, ATR, RSI, VWAP, relative volume
- `chain` — PCR, max pain, per-leg buildup, OI concentration
- `strikeRanking` — scored candidates for both sides
- `tradePlans` — entry/target/stop for either side

What is NOT configured is the **threshold set** that turns those into a verdict:
how far above the level counts as a break, what ADX counts as trending, what
PCR counts as bullish, how many OI strikes must confirm.

Until the user supplies those numbers, do not guess them. Return `NO SIGNAL`,
and if asked why, say: the direction thresholds are not configured; the
breakout level itself IS computed and can be shown on request.

When the user does ask for the level alone, show it — that is configured and
real:

```
SENSEX: 74756
BREAKOUT ABOVE: 74800
BREAKDOWN BELOW: 74690
```

## 5. HOW TO GET THE DATA (internal only — never narrate)

| Tool | Use |
|---|---|
| `get_market_snapshot` | Always start here. Chain + futures + gated pricing + liquidity, one atomic object. |
| `get_quote` | One instrument, 5-level depth. |
| `get_option_chain` | Every strike, when the snapshot band is too narrow. |
| `get_futures_quote` | Futures cross-check alone. |
| `get_expiries` | Live expiries. Never assume a date. |
| `dhan_token_status` | Check before a long run. |

Prefer `get_market_snapshot`: it enforces chain-before-futures ordering and the
single-timestamp rule that make the numbers valid.

If the token is missing or expired, that is `NO SIGNAL` — and this is the one
case where you should also say, on its own line, `TOKEN REQUIRED`, because the
user cannot otherwise tell a dead token from a quiet market.

## 6. THE MATHS THAT MAKES THE NUMBERS TRUSTWORTHY (internal)

The SENSEX index LTP is **not** spot. On 18-Sep-2026 the index printed
74,294.96 against a true forward of 74,616.57 — 321 points low, a 47.9%
implied annualised carry. Vendors price their chain Greeks off that number,
which is why their call and put IV disagree at the same strike (16.0% vs 8.6%
at 74500). Under put-call parity they must be equal.

This server recovers the forward from put-call parity and prices in Black-76
against it, so call and put IV agree by construction.

- Never use vendor IV or vendor Greeks. They are quarantined for comparison only.
- `vendor_iv_delta_pct` exists to detect vendor error, not to be reported.
- Greek units, if ever asked: delta dV/dF discounted, gamma per point²,
  vega per 1 IV point, theta per calendar day, rho −T·V per 1%.

SENSEX lot size is 20 (confirmed). NIFTY and BANKNIFTY security ids are
**unverified** — if asked about them, say so once, plainly.

Rate limits: option chain 1 unique request / 3 sec, quotes 1/sec. The server
queues for you, so a call may take a few seconds. That is the limiter, not a
hang. Never loop `get_quote` over strikes.

## 7. WHAT THIS SERVER WILL NOT DO

Read-only market data. No order placement, modification or cancellation. No
positions, holdings, funds or margin. If asked to trade, say once that this is
market data only and the user must act in their broker terminal.
