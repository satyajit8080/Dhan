# Pre-registration: H1a and H2 (written 1 Nov 2026, before any run)

This file is committed to git before the code that tests it is written or
run. Nothing below may change after the first run. If a rule turns out to be
unimplementable, the test is reported as failed, not re-specified.

Source of the hypotheses: the external audit
(`Bull50_SENSEX_option_buying_audit.md`, 31 Oct 2026), H1a and H2, adapted only
where our data forces it (marked **[data]**).

## Variant budget

20 trials in total. 7 already used (4 scalper variants and 3 ORB variants on
Apr–Oct 2026). This round adds **2: H1a and H2 → 9 of 20 used.** Re-pricing old
trades with the corrected cost model is a repair, not a trial.

## Data and windows

- SENSEX 1-min index bars and Dhan expired-weekly options (ATM−5..ATM+5, CE and
  PE, expiry codes 1 and 2), 20 Oct 2025 – 29 Oct 2026, as already downloaded.
- Test window: **27 Oct 2025 – 29 Oct 2026** (the whole year; this is Stage 2
  screening on already-seen data, so it can falsify but not confirm).
- **Expiry day [data]:** we have no historical contract list. A day is an
  expiry day if the code-1 ATM straddle's time value at the day's last bar,
  `CE + PE − |S − K|`, is **≤ 50 index points**. The list is printed and
  compared with the Thursday rule.

## Common rules

- **Contract:** pinned at entry (strike and expiry), marked only to that
  strike's own minute bars.
- **Fills [data]:** no historical bid/ask, so each fill pays a half-spread
  on top of the 1-min price: buys pay `price + h`, sells receive `price − h`.
  - Base: `h = max(0.6% × price, ₹0.10)`; on 0DTE, `max(1.0% × price, ₹0.10)`.
  - Stress: `h = max(1.0% × price, ₹0.10)`; on 0DTE, `max(1.5% × price, ₹0.10)`.
- **Charges per order** (1 lot = 20):
  - brokerage ₹20;
  - BSE transaction charge 0.0325% of premium turnover;
  - SEBI fee ₹10 per crore of turnover;
  - GST 18% on brokerage + exchange charge + SEBI fee;
  - stamp duty 0.003%, buy orders only;
  - STT on sell orders only: 0.10% of premium before 1 Apr 2026, 0.15% from
    1 Apr 2026.
- **Bars:** decisions only on completed bars; entries at the next minute's
  open; no OI inputs.
- **Flat time:** 15:15, or **15:10 on expiry days** (clears the closing
  auction start).
- **P&L unit:** net ₹ per lot per trade, and per day.

## The ORB signal (shared by H1a, H2 and the baseline)

- Range: high and low of 09:15–09:29.
- A 15-min candle (09:30–09:44, 09:45–09:59, …) that **closes** above the
  range high is a CE signal; one that closes below the range low is a PE
  signal.
- Signals until the 13:30 candle close; at most one per side per day; one
  position at a time.
- Strike: ATM = signal-bar close rounded to the nearest 100.
- Contract: nearest weekly (code 1). On an expiry day that is the expiring
  contract (0DTE).

## Baseline (repair, not a trial)

The naked ORB from the 31 Oct run, re-run with the corrected expiry list,
fills and charges:
- stop −25% of premium;
- once a 1-min close reaches +25%, the stop moves to breakeven, then trails 15%
  below the highest 1-min close;
- the stop fills at the stop price, or at the bar open if it gaps through.

The 1-min scalper and its variants are re-priced from their existing trade
lists with the new fills and charges.

## H1a — expiry-day ORB gated on cheap implied move

- Universe: expiry days only, 0DTE ATM options.
- `IM` = mean over 09:29, 09:30 and 09:31 of the 1-min closes of (ATM CE +
  ATM PE), ATM taken at the 09:29 close **[data: closes, not mids]**. The gate
  is decided at 09:32, before the first possible signal (09:44).
- `RV` = median, over the previous 10 expiry days in the data, of
  `|SENSEX(15:15 close) − SENSEX(09:30 close)|`. Days with fewer than 10 prior
  expiry days are excluded.
- `Q = IM / RV`. **Trade the day only if Q ≤ 1.00.**
- Signal, entry and exits: as the baseline, flat 15:10.
- Control: the ungated expiry-day ORB on the same eligible days. Report P&L by
  Q tercile.
- **Mechanism test:** Spearman correlation between Q and the ungated
  expiry-day ORB's daily net P&L, over eligible days. The mechanism is
  supported only if r < 0 with one-sided p < 0.10.

## H2 — the ORB signals traded as a debit spread

- Signals: identical to the baseline, all days.
- Structure: CE signal → buy ATM CE, sell the CE at ATM + 200 (two strikes
  OTM), same expiry; PE signal → buy ATM PE, sell the PE at ATM − 200.
- Entry: both legs at the next 1-min open, long leg paying the half-spread,
  short leg receiving minus it. Debit `D` = long fill − short fill.
- Mark each minute: long close − short close.
- Exits, checked on each 1-min close, filled at the next minute's open:
  - mark ≤ 0.70 × D → exit;
  - mark ≥ 0.80 × 200 = 160 → exit;
  - flat time;
  - no trailing.
- Costs: 4 orders per trade, STT on both sold legs.
- If either leg has no price at entry, the signal is skipped and counted.
- Report: standalone net, and the per-signal paired difference
  (spread − naked baseline), each with and without the top 5 days.

## Pass rules (Stage 2 screening), applied to H1a and H2 separately

All of these must hold, or the hypothesis fails and is dropped:

1. Net > 0 under base fills, and net ≥ 0 under stress fills.
2. It beats a random-entry control: 1,000 runs that keep each trade's day,
   side and exits but draw the entry minute uniformly from the allowed signal
   minutes (09:44–13:30 candle closes). `p = (k + 1) / 1001`, where `k` is
   the number of runs with net ≥ actual. Pass if p < 0.10.
3. Net stays > 0 after removing the best calendar month.
4. The top 5 days make up < 50% of net.

H1a additionally requires the mechanism test to pass. Passing Stage 2 earns
only a NIFTY replication and the multi-year holdout (both need new data), never
capital.

## Stop rule

If the corrected baseline ORB loses more than ₹50 per trade under base fills
**and** neither H1a nor H2 passes, intraday SENSEX option-buying research
stops.

---

# Pre-registration: H3 — hedged expiry-day iron fly (written 1 Nov 2026, before any run)

Committed before the code that tests it. Same rules as above apply: nothing
changes after the first run.

## Variant budget

This adds **1 trial → 10 of 20 used.**

## Data and expiry days

- Same data and test window: 27 Oct 2025 – 29 Oct 2026.
- **Expiry day:** the code-1 cheaper ATM leg closes at ≤ ₹1 at the day's last
  bar, or the straddle time value ≤ 50 pts. This is the detector validated in
  the H1a/H2 robustness check: it reproduces every Thursday plus the four
  known shifted expiries, so it is fixed here before H3 runs.

## Rules

- Universe: expiry days only, 0DTE (expiry code 1).
- **Setup at the 09:44 bar close:** ATM `K` = SENSEX close rounded to the
  nearest 100. Straddle `W` = ATM CE close + ATM PE close. Wings at the strikes
  nearest `K + W` (CE) and `K − W` (PE), rounded to 100.
- **If a wing is more than 500 pts from K, or any of the 4 legs has no price at
  entry, skip the day and count it** (our data holds ATM±5 strikes only).
- **Entry at the 09:45 open:** sell 1 lot ATM CE and ATM PE, buy 1 lot of each
  wing. Credit `C` = (short fills) − (long fills), using the fill model.
- **Mark** each minute from 1-min closes (a leg with no bar keeps its last
  close): loss = cost to close at closes − C.
- **Exits:**
  - stop when the marked loss ≥ 1.0 × C on a 1-min close, filled at the next
    minute's open;
  - otherwise all legs close at the 15:10 bar's close;
  - no adjustments, no re-entry.
- **Fills:**
  - 0DTE half-spread on every leg: base `max(1.0% × price, ₹0.10)`, stress
    `max(1.5% × price, ₹0.10)`;
  - buys pay price + h, sells receive price − h.
- **Charges:** 8 orders per day, as above (brokerage, BSE, SEBI, GST, stamp on
  buys, STT on every sell by date).
- **Margin:** no historical margin data exists. P&L is reported per set of
  4 lots, plus the worst day; return on capital is not claimed.

## Comparison

- Flat (0).
- The ungated naked expiry-day ORB on the same days (from the corrected
  baseline).

## Pass rules (Stage 2), all required

1. Net > 0 under base fills, and ≥ 0 under stress fills.
2. It beats a random-entry control: 1,000 runs with the same days and rules
   but an entry minute drawn uniformly from 09:45–13:30.
   `p = (k + 1) / 1001`; pass if p < 0.10. The iron fly takes no side, so this
   control does not leak direction.
3. Net stays > 0 after removing the best calendar month.
4. The top 5 days make up < 50% of net.

Also reported (not pass rules): win rate, average win and loss, worst day,
max drawdown, and P&L by month.

Passing earns only a NIFTY replication and the multi-year holdout, never
capital.
