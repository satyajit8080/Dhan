I trade SENSEX weekly options (BSE, lot 20) and NIFTY (lot 65). Below is every strategy I tested, each with its exact
logic, how it was tested and the result. Please review each one separately: point out flaws in the logic or the test,
say whether the conclusion is right, and suggest anything I have not tested that has real evidence behind it.

==================================================================
COMMON TEST SETUP (applies to every test unless stated)
==================================================================
- Data (Dhan API, read-only): SENSEX 1-min index 27 Oct 2025 – 26 Oct 2026 (247 days); real expired nearest-weekly
  SENSEX ATM CE/PE 1-min premiums with OI (rolling ATM series; when the ATM strike changes, the held strike's price is
  bridged with delta 0.5); 3.4 years of SENSEX minute prices (May 2023 – Oct 2026) for longer tests; 50 NIFTY / 30
  SENSEX stocks with their nearest-monthly ATM CE/PE premiums, OI and volume; NIFTY 1-min index and weekly ATM options.
- Costs: SENSEX option ₹70 per trade (≈ 3.5 pts) plus 0–4 pts slippage on stops; ~5 pts per round trip total;
  SENSEX future ~22 pts; NIFTY option ~1.5 pts, NIFTY future ~13 pts. STT on options is 0.15% of premium (sell side).
- Validation: year split into halves (before / after 28 Apr 2026); rules chosen on the first half and checked on the
  second; a "chance baseline" = the same search on outcomes randomly sign-flipped per day.
- Levels: support/resistance from today's 5-min candles (swing highs/lows, opening range, session high/low; prices
  within ~22 pts grouped); a level needs >= 2 touches; trigger = level + 5 (CE) / − 5 (PE), rounded to 5.

==================================================================
1. FIRST_TOUCH (base breakout)
==================================================================
Logic: buy the ATM CE when SENSEX crosses a resistance trigger, ATM PE when it crosses a support trigger; exit +6 / −11
premium or 10 minutes.
Test: 5,105 crossings in 1 year, real premiums.
Result: 49.9 % wins (break-even 64.7 %); −₹6.1 lakh per lot (−₹8.2 lakh with 4-pt stop slippage). Both halves lose.

==================================================================
2. FILTERED (live paper variant)
==================================================================
Logic: FIRST_TOUCH plus all of: day trend (futures above today's open for CE, below for PE; futures price must be
< 120 s old), fresh level (< 3 touches), today's range >= 300 pts, strike not more than 150 pts in the money.
Option data is used only for pricing (entry at ask, exit at bid), not for the decision.
Test: 782 crossings in 1 year (trend measured on the index vs its open, as futures history was not available).
Result: 50.0 % wins; −₹93,955 per lot. Range 200 / 400 instead of 300: still negative both halves.
Live paper (21–28 Oct, 6 days): 53 trades, 72 % wins, +₹6,940 before charges — but one breakout opens several
strikes, so it is ~15–20 independent events, in a trending week.

==================================================================
3. FILTERED + extra filters, target 80 % win rate
==================================================================
Logic: FILTERED (or all crossings) plus combinations of up to 3 yes/no filters:
- option data: high IV (ATM straddle vs its 20-day median), IV rising over 15 min, ATM put/call OI ratio on the trade
  side (local PCR), ATM OI flow (put writing vs call writing), expiry day, day before expiry. (Full-chain PCR and
  max pain could not be tested: no full-strike OI history.)
- breakout: calm approach (<= 15 pts in the 3 min before), new day high/low, crossing minute closed beyond trigger,
  before 11:30, exactly 2 touches.
- weighted stocks: breadth on the trade side, 5-min weighted move of 30 stocks, top-3 heavyweights agreeing.
Exits: +6/−11, +4/−11, +3/−11, +6/−15.
Test: 16,720 combinations, chosen on the first half.
Result: a first run showed 7 combinations at 80 %+ in both halves, but all used "the crossing minute closed beyond the
trigger" while entering inside that minute (look-ahead). After fixing (enter at that minute's close): 0 combinations at
80 %+ in the first half, 1 at 70 %+ in both halves, 0 profitable in both halves.
Best first-half pick: all crossings + weighted basket NOT agreeing + not day before expiry + IV rising, exit +4/−11:
first half 30 trades, 77 % wins, −₹1,800; second half 34 trades, 56 %, −₹4,160 (a win is +₹10 after charges, a loss
−₹290, so break-even is ~73 %).

==================================================================
4. FRESH_EXTREME / FRESH_CALM / BREAK_RETEST
==================================================================
Logic: FRESH_EXTREME = fresh level (2 touches) whose trigger is a new session high/low. FRESH_CALM = same, not expiry
day, index moved <= 15 pts into the break over the 3 prior minutes. BREAK_RETEST = fresh break, a 1-min close beyond,
a retest of the trigger within 5 min with no close back through the level, then entry when price trades beyond the
retest bar.
Result (real premiums, 1 year): FRESH_EXTREME 49 % wins, −₹1.7 lakh; FRESH_CALM 55 %, −₹33k; BREAK_RETEST 56 %, −₹37k.
On an index-only model (delta 0.5) FRESH_CALM showed 70 % — real premiums cut it to 55 %.

==================================================================
5. Scalping rules v3 (another chat's rules)
==================================================================
Logic: 1-min close beyond the trigger and the next candle holds; stop = 1-min close back through the level; target 1.5×
stop distance; 10-min time stop; max 3 losing trades a day; skip expiry day.
Result: 1,093 trades, 49 % wins, −₹1.28 lakh. 4,096 variants (option vs future, stops, targets, holds, 16 filters):
0 profitable in either half.

==================================================================
6. Broad intraday rule search
==================================================================
Logic: one decision per day at fixed times (every 15 min 09:30–14:45) using momentum 5–120 min, price vs open, gap,
previous day, range position, ATM OI change; follow and fade; holds 5–60 min or to close; filters by weekday, expiry,
IV regime; options or futures.
Result: 41,988 combinations; 3.4 % positive in both halves vs chance baseline 3.1–3.9 %. No option-buying rule survived.

==================================================================
7. Next 1-min / 5-min candle prediction
==================================================================
Logic: features at each minute (SENSEX moves, FRESH signals, SENSEX ATM CE−PE change, OI, 30 stocks' moves, breadth,
lead/lag gap, stock option premium/OI/volume); linear and gradient-boosting models, trained on the first half.
Result: index direction 58.5 % right next minute out-of-sample (most confident 10 %: 70 %), but buying the SENSEX ATM
option on it lost 2–5 pts per trade before costs. Explanation (confirmed by 3 external reviews): SENSEX is computed from
BSE prints and lags NSE prices and options by ~1.5–4 s; tradeable options have already moved. NIFTY (computed from NSE)
showed only 53 %.

==================================================================
8. Final walk-forward model (SENSEX and NIFTY)
==================================================================
Logic: 40 features every 5 min (bullish/weak state of each of 30 / 50 stocks vs open, previous close, 15 and 30 min;
SENSEX chart: momentum, RSI, MA slope, opening range, new high/low, range position, volatility, FRESH signals; index ATM
options: CE−PE, straddle, OI; stock options: CE−PE, straddle, OI, CE/PE volume; sectors; breadth; dispersion).
Entry one minute after the signal; monthly retraining on past months only; most confident 10 % traded.
Result: SENSEX 44–53 % right, NIFTY 46–53 %; chance baseline 46–52 %. Option buying negative at 15 / 30 / 60 min and to
close in both halves.

==================================================================
9. Breadth / sector / midday momentum
==================================================================
Logic: at 10:00–13:30 snapshots: broad move (breadth >= 70 % or <= 30 %) → follow; narrow move (top 3 stocks >= 60 %
of the move) → fade; IT vs banks opposite → follow banks; biggest sector → follow. Midday momentum: SENSEX > 0.6 % from
previous close at 12:30 → follow to 15:25.
Result: breadth rules 9 combinations > futures cost in both halves vs chance 3–17 (noise). Midday momentum direction
right 64 / 56 / 58 % in 2023-24 / 24-25 / 25-26 (+19 / +41 / +49 pts) but buying the ATM option lost 17–48 pts per
trade every year (decay over ~3 h); futures −3 / +14 / +26 pts after costs. Same rule at 12:00 failed.

==================================================================
10. Option premium spikes (SENSEX and 8 stocks)
==================================================================
Logic: buy the ATM CE/PE when its own premium jumps >= 3–15 % in 1–5 min (± volume spike, ± 15-min high).
Result: SENSEX 1,584 variants, 0 profitable; after a jump the premium falls (−5 % to −12 % over 10 min). Stocks: best
rule (stock breaks its 09:15–09:30 range → ATM option, +40 %/−10 %, 10 min) +0.5 % per trade on 6 stocks, negative on
2 unseen stocks.

==================================================================
11. "Fable" spike scalp (an AI agent's live trades on 27–28 Oct: 12 of 13 winners)
==================================================================
Logic (reconstructed from fills vs 1-min option prices): buy the ATM CE/PE right after a fast index move or level break,
sell 1–5 minutes later.
Test: index moved >= X pts in 1–2 min (SENSEX 40–100, NIFTY 12–33) → ATM option at that minute's close or the next;
exits 1–5 min or small targets; all days / expiry / day before.
Result: SENSEX 1,559 trades 47 % wins before costs, −₹1.6 lakh per lot; NIFTY 771 trades 48 %, −₹60k. 1 of 504
variants positive in both halves.

==================================================================
12. Nearest-level pick (another chat's 73300 PE call)
==================================================================
Logic: trade only the nearer level's trigger; PE needs < 50 % of 30 stocks above previous close and SENSEX below its
previous close (CE the reverse); ATM option at the crossing.
Result: 2,573 trades, 50 % wins, −₹3.06 lakh (+6/−11); +10/−10 and +20/−10 also negative both halves.

==================================================================
13. Volatility selling (the only profitable family)
==================================================================
Logic: sell the delta-0.10 CE and PE one trading day before weekly expiry at the 09:30 close, hold to expiry.
Condor adds wings 1,000 pts further out.
Result (3.4 years, 1 lot): strangle 91 % weekly win, +₹1.39 lakh, worst week −₹11,477 (unlimited risk); condor 90 %,
+₹88,419, worst week −₹11,901 (defined risk). Intraday variants: overnight short straddle +₹46,732 but first half
negative and one night −₹32,000; expiry-day 14:30 short straddle +₹11,560 / +₹2,711 (52 trades). Long straddles
(buying volatility) lost except 31 trades on wide-open days.

==================================================================
QUESTIONS
==================================================================
1. For each section: is the logic or test flawed (look-ahead, selection bias, cost model, rolling-ATM bridging,
   one-minute bars)? Is the conclusion right?
2. Does the live FILTERED result (72 % on ~15–20 independent breakouts in 6 days) contradict the 1-year 50 %? How many
   independent events are needed to tell?
3. Is there published evidence of any intraday option-BUYING edge on Indian index options after current costs?
4. For the condor: what are the main risks (gap weeks, regulation of weekly expiries, STT changes, margin), and how
   should it be sized for ₹20 lakh?
5. What should I test next that is most likely to be real?
