---
description: Gated SENSEX snapshot — forward, Greeks and executable liquidity
argument-hint: "[expiry YYYY-MM-DD] [lots]"
allowed-tools: mcp__bull50-dhan__get_market_snapshot, mcp__bull50-dhan__dhan_token_status, mcp__bull50-dhan__get_expiries
---

Produce one atomic, gated SENSEX snapshot for options scalping.

Arguments: $ARGUMENTS
(first value, if it looks like YYYY-MM-DD, is the expiry; a bare integer is lots)

Steps:

1. Call `get_market_snapshot` with underlying SENSEX, `include_depth: true`, and
   the lots given (default 1). Pass the expiry only if one was supplied —
   otherwise let the server resolve the nearest live expiry.

2. **If `published` is false, stop.** Report the block reasons verbatim and say
   plainly that no pricing was produced. Do not estimate, do not substitute the
   index LTP, do not "approximate while we wait". A blocked gate means the data
   was not trustworthy enough to trade on, and that is the answer.

3. If published, lead with the pricing integrity picture, because it is what
   makes the rest meaningful:
   - parity forward vs listed future, and the divergence against its 75-point limit
   - per-strike parity spread against its 40-point limit
   - the index LTP, the index divergence, and — stated explicitly — that it is
     not used as spot
   - snapshot skew and whether both legs shared a fetch id

4. Then the near-ATM legs: strike, type, LTP, our IV, delta, theta/day, vega.
   Where `vendor_iv_delta_pct` is large, say so and say which is wrong and why.
   Our IV is computed against the parity forward; the vendor's is computed
   against the index LTP, so on a day with meaningful carry the vendor's call
   and put IV at the same strike will disagree and ours will not.

5. Then liquidity. For each graded candidate give the round-trip cost at the
   requested size and the slippage multiple over the quoted spread. Call out any
   leg whose quoted spread looks tight but whose real round-trip at size does
   not — that gap is the entire reason Stage 2 exists.

6. Close with one short paragraph: which strikes are actually tradeable at the
   requested size, and what would change that.

Units, every time you quote a Greek: delta is dV/dF discounted, gamma is per
point squared, vega is per 1 IV point, theta is per calendar day, rho is per 1%.
Never present a vendor Greek as if it were ours.
