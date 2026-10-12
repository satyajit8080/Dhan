# 12 Oct 2026 — where the PAPER losses came from (facts from the Cloud log)

Data: 95 paper trades = 19 trigger-crossing events (one trade per configured strike per crossing).
Rule tested: entry at the ASK on the FIRST poll at/through an OK trigger; exit +6 / −11 premium on the BID; 5-s polls.
Script: `tools/analyze_paper_events.py`. One session only — every figure below is a lead to test, not a result.

## 1. False breakouts (largest effect)
| Condition at the crossing | Events | Mostly TP | Paper P&L |
|---|---|---|---|
| The 1-min bar of the crossing **closed beyond** the trigger | 10 | 6 | **+104.2** |
| …it did not | 9 | 3 | **−337.6** |
| That close **and the next 1-min bar** also beyond ("next bar holds", RULES §6) | 8 | 6 | **+236.6** |
| …not | 11 | 3 | **−469.9** |

22 of 49 stopped trades were **never in profit** (max favourable 0); median time to stop 30 s — the touch reversed inside
the same minute. Caveat: P&L above is from the first-touch entry; an entry that waits for the close enters later at a
different premium, so its real result must be measured (see §5).

## 2. Strike drift (second effect)
SENSEX fell 72,390 → 71,680 while strikes stayed 72,200–72,600, so the PE legs became deep in the money:
| Moneyness at entry | Trades | P&L | Avg |
|---|---|---|---|
| ITM (> 50 pts) | 75 | −244.3 | −3.26 |
| ATM (±50) | 4 | −8.6 | −2.15 |
| OTM | 16 | +19.6 | +1.23 |
Deep ITM legs have |delta| ≈ 0.6–0.7: 11 premium ≈ 16–18 index points, inside one normal 1-min bar (30–50 pts).

## 3. Exit mechanics
- Realised stop average −13.51 (worst −20.25) vs nominal −11; realised target +9.31 vs +6 (5-s polling, premium gaps).
- Spread at entry p50 1.75 pts is paid immediately (entry at ask, exit at bid).
- Same recorded paths, tighter thresholds (exact on these paths; not an optimisation, one day only):
  6/11 −233.3 · 4/11 −211.9 · 6/8 −112.0 · 5/6 −159.2 · 4/6 −144.5 · 3/5 −119.9 — **every variant still negative**:
  exits alone do not fix first-touch entries.

## 4. Things that did NOT separate winners from losers today
- Futures above/below session VWAP in the trade direction (16 aligned events: −287.9).
- Level strength ≥ 3 touches (15 events: −161.2).
- Futures 1-min volume ≥ 1.5× the previous 10 bars (5 events: −34.8).
- Morning vs afternoon; CE vs PE per event.

## 5. Proposed observation changes (no rule changes; for the user's decision)
1. Run a **second paper variant in parallel**: enter only after a 1-min **close beyond** the trigger (D1), optionally
   requiring the next bar to hold (D2); same +6/−11; logged separately so first-touch vs confirmed is compared on the same days.
2. Keep strikes near the money (needs the user's P1 decision): either the user supplies strikes each morning, or an
   observation-only "follow ATM" option re-centres the 5 strikes every scan.
3. Optionally record the RULES §6 original exit (stop on a 1-min close back through the level, 10-min time stop)
   alongside +6/−11, to compare the two exit rules on identical entries.
