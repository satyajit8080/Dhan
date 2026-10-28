#!/usr/bin/env python3
"""Option BUYING on NSE stock options (real expired 1-min premiums, Dhan rolling ATM, nearest monthly), pooled over
liquid stocks. Local files only; no orders.

  python3 tools/backtest_stock_option_buying.py DATA_DIR

DATA_DIR holds <STOCK>_<CALL|PUT>_<from>.json payloads of /charts/rollingoption (ATM, MONTH, expiry code 1).
Strategies (one at a time per stock and side):
  A  premium breakout: the option's own close rose >= X % over N min (same strike), optional volume spike
     (volume of the signal minute > 3 x the 15-min average), optional close above its 15-min high
  B  stock breakout: the stock (spot field) crosses its 09:15-09:30 opening-range high -> buy ATM CE, low -> ATM PE,
     optional volume spike on the option at the crossing minute; first crossing per day per side
  C  wide-open day: the stock's 09:15-09:30 range > 1.5 x its 10-day average -> buy the ATM straddle at 09:45
Exits: target +10 / +20 / +40 %, stop -10 / -20 %, time stop 10 / 30 / 60 min or 15:15. No entries before 09:30 or
after 14:30. Strike rolls during the hold are bridged with delta 0.5. Costs per trade: 1.2 % of the entry premium
(about 1 % bid-ask round trip + ~0.2 % STT/exchange/stamp; brokerage per order not included because it depends on
the lot value). Results are in % of premium per trade; halves split at 28 Apr 2026, chosen on the first half.
"""
import datetime, glob, itertools, json, os, sys
from collections import defaultdict

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
COST = 1.2


def load(ddir):
    S = defaultdict(dict)                                    # (stock, side, day) -> {t: row}
    for f in glob.glob(os.path.join(ddir, "*.json")):
        name, side, _ = os.path.basename(f)[:-5].split("_")
        p = json.load(open(f))
        leg = p.get("ce" if side == "CALL" else "pe") if isinstance(p, dict) else None
        if not leg or not leg.get("timestamp"):
            continue
        for i, t in enumerate(leg["timestamp"]):
            dt = datetime.datetime.fromtimestamp(t, IST)
            S[(name, "CE" if side == "CALL" else "PE", dt.date().isoformat())][t] = (
                dt.strftime("%H:%M"), leg["strike"][i], leg["open"][i], leg["high"][i], leg["low"][i], leg["close"][i],
                (leg.get("volume") or [0] * len(leg["timestamp"]))[i], leg["spot"][i])
    return {k: [(t,) + v[t] for t in sorted(v)] for k, v in S.items()}   # (t, hm, K, o, h, l, c, vol, spot)


def run_exit(s, i, tp, sl, hold, sign):
    K = s[i][2]; e = s[i][6]
    if e <= 0:
        return None
    last = e
    for k in range(i + 1, len(s)):
        t, hm, k2, o, h, l, c, v, sp = s[k]
        if (hold != "eod" and k > i + hold) or hm >= "15:15":
            break
        sh = 0.0 if k2 == K else 0.5 * (k2 - K) * sign
        h, l, c = h + sh, l + sh, c + sh
        if l <= e * (1 - sl):
            return -100 * sl - COST
        if h >= e * (1 + tp):
            return 100 * tp - COST
        last = c
    return 100 * (last - e) / e - COST


def sig_premium(s, N, X, vol, brk):
    out, busy = [], -1
    for i in range(15, len(s)):
        hm = s[i][1]
        if i <= busy or hm < "09:30" or hm > "14:30" or any(s[j][2] != s[i][2] for j in range(i - 15, i + 1)):
            continue
        c0 = s[i - N][6]
        if c0 <= 0 or (s[i][6] - c0) / c0 < X:
            continue
        if vol and s[i][7] <= 3 * (sum(s[j][7] for j in range(i - 15, i)) / 15 or 1e9):
            continue
        if brk and s[i][6] <= max(s[j][6] for j in range(i - 15, i)):
            continue
        out.append(i); busy = i + 10
    return out


def sig_stock_break(s, side, vol):
    orh = [x for x in s if x[1] < "09:30"]
    if len(orh) < 5:
        return []
    hi, lo = max(x[8] for x in orh), min(x[8] for x in orh)
    for i in range(1, len(s)):
        hm = s[i][1]
        if hm < "09:30" or hm > "14:30":
            continue
        crossed = s[i][8] > hi and s[i - 1][8] <= hi if side == "CE" else s[i][8] < lo and s[i - 1][8] >= lo
        if crossed:
            if vol and s[i][7] <= 3 * (sum(s[j][7] for j in range(max(0, i - 15), i)) / 15 or 1e9):
                return []
            return [i]
    return []


def main():
    S = load(sys.argv[1])
    stocks = sorted({k[0] for k in S}); days = sorted({k[2] for k in S}); half = days[len(days) // 2]
    print("stocks %s | days %d | series %d | halves split at %s" % (",".join(stocks), len(days), len(S), half))
    exits = list(itertools.product((0.10, 0.20, 0.40), (0.10, 0.20), (10, 30, 60, "eod")))
    res = []

    def collect(name, sigs):
        for tp, sl, hold in exits:
            h = [[], []]
            for (stock, side, d), idx in sigs.items():
                s = S[(stock, side, d)]
                for i in idx:
                    p = run_exit(s, i, tp, sl, hold, 1 if side == "CE" else -1)
                    if p is not None:
                        h[d >= half].append(p)
            if len(h[0]) >= 30 and len(h[1]) >= 30:
                a = h[0] + h[1]
                res.append((sum(h[0]) / len(h[0]), sum(h[1]) / len(h[1]), name, tp, sl, hold, len(h[0]), len(h[1]),
                            100 * sum(p > 0 for p in a) / len(a)))

    for N, X, vol, brk in itertools.product((1, 3, 5), (0.05, 0.10, 0.15), (False, True), (False, True)):
        collect("A premium +%d%% in %d min%s%s" % (100 * X, N, " +volume" if vol else "", " +15m high" if brk else ""),
                {k: sig_premium(s, N, X, vol, brk) for k, s in S.items()})
    for vol in (False, True):
        collect("B stock opening-range break%s" % (" +volume" if vol else ""),
                {k: sig_stock_break(s, k[1], vol) for k, s in S.items()})
    # C: wide-open day straddle at 09:45 (both legs counted as one trade each)
    rng = {}
    for (stock, side, d), s in S.items():
        if side == "CE":
            o = [x[8] for x in s if x[1] < "09:30"]
            if o:
                rng[(stock, d)] = max(o) - min(o)
    sigC = {}
    for stock in stocks:
        ds = sorted(d for (st, d) in rng if st == stock)
        for n, d in enumerate(ds[10:], 10):
            avg = sum(rng[(stock, x)] for x in ds[n - 10:n]) / 10
            if avg > 0 and rng[(stock, d)] > 1.5 * avg:
                for side in ("CE", "PE"):
                    s = S.get((stock, side, d)) or []
                    i = next((i for i, x in enumerate(s) if x[1] >= "09:45"), None)
                    if i is not None:
                        sigC[(stock, side, d)] = [i]
    collect("C wide-open day straddle 09:45", sigC)

    print("combinations (>= 30 trades per half): %d | positive avg after costs: H1 %d, H2 %d, BOTH %d"
          % (len(res), sum(r[0] > 0 for r in res), sum(r[1] > 0 for r in res), sum(r[0] > 0 and r[1] > 0 for r in res)))
    fmt = "%-44s TP %2.0f%% SL %2.0f%% hold %-4s | n %4d/%4d win %4.1f%% | avg per trade H1 %+6.2f%% / H2 %+6.2f%%"
    print("\nTop 15 chosen on the FIRST half -> second half:")
    for r in sorted(res, key=lambda r: -r[0])[:15]:
        print(fmt % (r[2], 100 * r[3], 100 * r[4], r[5], r[6], r[7], r[8], r[0], r[1]))
    both = sorted([r for r in res if r[0] > 0 and r[1] > 0], key=lambda r: -min(r[0], r[1]))
    print("\nPositive in both halves (best 15 by the weaker half):")
    for r in both[:15]:
        print(fmt % (r[2], 100 * r[3], 100 * r[4], r[5], r[6], r[7], r[8], r[0], r[1]))
    print("\nFollow-through after a sudden premium jump (no exits, no costs):")
    for N, X, vol in ((1, 0.05, False), (3, 0.10, False), (3, 0.10, True), (5, 0.15, True)):
        for H in (5, 10, 30):
            v = []
            for k, s in S.items():
                for i in sig_premium(s, N, X, vol, False):
                    if i + H < len(s) and s[i + H][2] == s[i][2] and s[i][6] > 0:
                        v.append(100 * (s[i + H][6] - s[i][6]) / s[i][6])
            if v:
                print("  +%2.0f%% in %d min%-8s -> after %2d min: avg %+.2f%%, higher in %.0f%% of %d"
                      % (100 * X, N, " +vol" if vol else "", H, sum(v) / len(v), 100 * sum(x > 0 for x in v) / len(v), len(v)))


if __name__ == "__main__":
    main()
