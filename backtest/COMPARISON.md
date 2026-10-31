# Eight external reviews compared, and the decision (1 Nov 2026)

Reviews: Audit (`Bull50_SENSEX_option_buying_audit.md`), Grok, Gemini, Meta,
ChatGPT, GLM, Muse, DeepSeek. Kimi pending.

## Where all eight agree

1. **No-go on naked intraday SENSEX option buying.** 8 of 8.
2. **₹10k/day on ₹1 lakh is not a plannable target.** 8 of 8.
3. **The scalper's gross edge (+₹3.3/trade over 767 trades) is noise**, so no
   filter or stop change can rescue it. 8 of 8.
4. **ORB's expiry-day "edge" is one month (Oct 2026)** and proves nothing. 8 of 8.
5. **Multiple testing** on the same Apr–Oct window inflates the best variant. 8 of 8.
6. **We need real bid/ask data.** Historical quotes do not exist in Dhan's API;
   they must be logged live. 8 of 8.
7. **Next research belongs on NIFTY and/or defined-risk / hedged selling**, not
   on more SENSEX buying variants. 8 of 8 in some form.

## Claims checked against our own data

| Claim | Who | Verdict |
|---|---|---|
| Held options may be stitched across strikes (ATM-relative data) | Audit #1, GLM #1, ChatGPT #3 | **Resolved: not a bug.** Delta-slope 0.43 / 0.50, R² 0.75 / 0.90 (RESULTS.md). |
| SENSEX expiry was Tuesday (or Friday) for most of our window | Meta #1, Muse #4, Gemini #7 | **Wrong for our window.** It moved to Thursday from 1 Sep 2025; our window starts 27 Oct 2025. The data-derived detector finds Thursday expiries all year, except 4 known shifts (24 Dec, 14 Jan, 25 Mar, 27 May). |
| STT is 0.10% now | Gemini, Muse | **Wrong.** 0.15% on sell premium since 1 Apr 2026 (Finance Act 2026); our corrected tests use it by date. |
| Stop at next-minute open after a close-through is lookahead in our favour | Meta #3 | **Wrong.** It uses only completed bars and fills after the signal; DeepSeek and GLM also call it correct or conservative. |
| Target fill at the bar close on an index touch is optimistic | DeepSeek #2, ChatGPT, Meta | **Fair.** It can only make results worse; it does not change any verdict. |
| ORB's "unseen" half came *before* the tuning half | ChatGPT, GLM | **Fair.** The holdout is time-inverted; treat it as weak evidence. |
| Real spreads are 0.5–1%+ per side (Meta, Muse), 2–5 pts (Gemini), ~0.3% (Grok), "calm ATM fills cheaper than 0.3%" (GLM) | — | **Measured.** 215 live quotes on 30 Oct 2026 (non-expiry, premium ~₹600): **full spread median 0.26%, p90 0.29%, max 0.38% (1.5 pts)**, so the half-spread is ~0.13% per side. GLM and Grok were closest; Meta, Muse and Gemini overstated it 4–8×. Expiry-day (0DTE) spreads are still **unmeasured**. |

## What the measured spread changes

Buying strategies: **nothing.** Even at zero slippage, with real charges
(₹62–66 per round trip), the scalper loses ~₹60/trade and the ORB's gross is
already negative (−₹9.6/trade).

The iron fly (H3) is the one result that depends on spread. A post-hoc sensitivity
re-run (`HALF_SPREAD_PCT`, `results/h3_sensitivity_*`; it does **not** change
the registered verdict):

| Half-spread per leg | Net / year | Per expiry day | Without best month | Green months | Top 5 days |
|---|---|---|---|---|---|
| 1.0% (registered) | +₹2,237 | +₹49 | −₹3,916 | — | ₹16,845 |
| 0.5% | +₹6,129 | +₹133 | −₹366 | 8 / 13 | ₹17,192 |
| 0.26% | +₹7,982 | +₹174 | +₹1,325 | 9 / 13 | ₹17,356 |
| **0.13% (measured, non-expiry)** | **+₹8,928** | **+₹194** | **+₹2,187** | **9 / 13** | ₹17,437 |

Even at the measured spread it still fails the concentration rule (its top 5 days are
about 2× the net) and its entry time does no better than random entry (p 0.28). It is
46 trades. The open question is **0DTE spreads on expiry day**: below roughly
0.5% per leg the iron fly turns positive in most months; above 1% it is flat.

## Where the reviews disagree, and the call

| Question | Range of views | Decision |
|---|---|---|
| Trial budget left on this SENSEX year | Muse 3, Grok 6–8, ChatGPT 7, GLM 8, DeepSeek 8 (16 tests), Gemini 12, Audit 20 | **Exhausted.** We have used 10. Under the majority of protocols, nothing more run on this year of SENSEX data can count as evidence. New evidence must come from **new data**: forward logs or NIFTY. |
| Debit spreads | Grok, Gemini, DeepSeek: test them | **Done, failed** (H2: −₹414/trade, worse than naked). Closed. |
| Keep researching buying? | ChatGPT: continue 15-min longer holds; GLM: one last shot; others: stop | **Stop buying research on SENSEX.** The ORB gross is negative over the year; longer holds would be variant #11 on the same data. |
| Hedged selling | Most: test as comparison; Meta: switch to it | **The one live lead**, but post-hoc. It earns a forward test, not capital. |

## Decision

1. **No capital on any strategy tested.** Unanimous.
2. **Close SENSEX intraday option buying.** Scalper, ORB, filters and spreads are done.
3. **Do not run more variants on the 27 Oct 2025 – 29 Oct 2026 SENSEX data.**
4. **Next, in order (each needs a fresh Dhan token):**
   1. **Log expiry-day bid/ask live.** Extend the observer to record top-of-book
      for the 0DTE ATM ±600 strikes every minute on expiry days. 4–6 expiries
      settle the one number the iron fly depends on.
   2. **Forward paper-test the frozen H3 iron fly**, unchanged rules, real
      quotes, minimum 20 expiries before any judgement. Kill it if net is
      negative after the 0DTE spreads are measured.
   3. **Replicate frozen H3 on NIFTY's multi-year history** (Tuesday expiries,
      lot 65/75, NSE charges) as the out-of-sample test. One trial; pass/fail
      rules as in PREREGISTRATION.md.
5. **Stop for good** if the forward iron fly or the NIFTY replication fails.
