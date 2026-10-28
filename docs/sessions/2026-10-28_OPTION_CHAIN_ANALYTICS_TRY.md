# Trying ArturSepp/OptionChainAnalytics on the live SENSEX chain (28 Oct 2026)

Library: `option-chain-analytics` (PyPI, Artur Sepp). It is a **data-container layer** — it normalises option feeds into
one schema, rebuilds a chain at a point in time and runs quote/parity diagnostics. It has no strategies, signals or
back-tests and no Indian/Dhan adapter. Adapter written: `tools/oca_sensex_snapshot.py` (read-only Dhan chain → OCA
`OptionsDataDFs`; IV/delta from our Black-76 on bid/ask mids with the parity forward; lot 20; expiry 15:30 IST).

Live run 10:31 IST (spot 72,670):

| Expiry | Forward | ATM vol | ATM straddle | 25Δ call / put vol | 25Δ risk reversal | 25Δ fly |
|---|---|---|---|---|---|---|
| 29 Oct | 72,696 | 24.3 % | 811 (±1.11 %) | 73,400: 25.2 % / 72,000: 24.1 % | **+1.08** (calls dearer) | 0.36 |
| 5 Nov | 72,730 | 18.7 % | 1,627 (±2.24 %) | 74,100: 18.4 % / 71,400: 19.1 % | −0.77 | 0.06 |

12 Nov skipped: fewer than 10 options with a valid quote per side (not trading yet).

Quote diagnostics (5 % relative-spread limit): 29 Oct median spread 0.75–0.95 pts (≈ 1 %), 23 calls / 10 puts fail —
mostly deep in-the-money calls with no open interest and spreads of ₹1,000–2,000. 5 Nov is wider (median 11–14 pts).

Reading: the front week is ~5.6 vol points above the next week (expiry-day event premium after a ~600-pt fall from
73,280), and the call wing is priced above the put wing — unusual for an index. These describe positioning and
cost of options; they are not trade signals. No orders.
