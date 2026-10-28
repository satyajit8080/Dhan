#!/usr/bin/env python3
"""Low-delta SENSEX premium selling (strangle / condor) with volatility, trend and adjustment rules.
3.4 years of 1-min SENSEX option data (rolling ATM+-10 = +-1,000 pts, smile-fitted; strikes beyond +-1,000 are
EXTRAPOLATED -- model prices, not traded prices). Local files only; no orders.

  python3 tools/backtest_premium_selling_dynamic.py PICKLE [PICKLE ...]

Base: sell the delta-0.10 call and put one trading day before each weekly expiry at 09:30, hold to settlement
(condor: buy wings 1,000 pts further out). Variants:
  vol (VIX proxy)  entry straddle vs the median of the previous 10 entries: HIGH (> 1.2x) / LOW (< 0.8x)
                   -> skip high, skip low, or sell delta 0.07 in high-vol weeks
  trend            index change over the 3 trading days before entry: |chg| > 1.5 % = trending
                   -> skip trending weeks / sell the trend side at delta 0.05 / sell only the side against the trend
  adjust           tested side = the index reaches the short strike (or 50 % of the way to it from entry):
                   close that side ("close") or close it and sell a new delta-0.10 strike on that side ("roll"),
                   at most 2 adjustments; each leg traded costs Rs 75 + 2 pts
Costs: strangle Rs 150 + 4 pts, condor Rs 250 + 6 pts per round; lot 20. Reported: trades, win %, net, worst week,
max drawdown, per year, first / second half. Capital: worst week and max drawdown are what the reserve must cover.
"""
import collections, pickle, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_wings as w                                   # noqa: E402

LOT = 20


def strike_for_delta(r, kind, t):
    K = None
    for k in range(1, 30):
        K = r["K"] + 100 * k if kind == "C" else r["K"] - 100 * k
        s = w.sig(r, K)
        d = w.Phi((r["F"] - K) / s) if kind == "C" else w.Phi((K - r["F"]) / s)
        if d <= t:
            break
    return K


def entry_row(R, cfg):
    """Entry minute on the entry day. Default 09:30. cfg['entry']:
         ('time', 'HH:MM')                  fixed time
         ('pullback', X, fallback)          the index first moves >= X % from the day open (either way), then gives
                                            back half of that move -> enter; none by 15:00 -> fallback ('skip' or 'HH:MM')
         ('spike', ratio, fallback)         ATM straddle >= ratio x its 09:30 value -> enter"""
    rule = cfg.get("entry", ("time", "09:30"))
    if rule[0] == "time":
        return next((r for r in R if r["hm"] >= rule[1]), None)
    o = R[0]["spot"]; ext = None; base = next((w.end_straddle(r) for r in R if r["hm"] >= "09:30"), None)
    for r in R:
        if r["hm"] < "09:30" or r["hm"] > "15:00":
            continue
        if rule[0] == "pullback":
            mv = r["spot"] - o
            if ext is None and abs(mv) >= rule[1] / 100 * o:
                ext = mv
            if ext is not None:
                if abs(mv) > abs(ext) and mv * ext > 0:
                    ext = mv
                if (mv / ext) <= 0.5:
                    return r
        elif rule[0] == "spike" and base and w.end_straddle(r) >= rule[1] * base:
            return r
    return None if rule[2] == "skip" else next((r for r in R if r["hm"] >= rule[2]), None)


def simulate(D, cfg):
    days = sorted(D); exps = sorted(w.EXP); out = []
    hist = []
    for e in exps:
        if e not in days:
            continue
        i = days.index(e)
        if i < 4:
            continue
        d0 = days[i - 1]
        r0 = entry_row(D[d0], cfg)
        if r0 is None:
            continue
        r930 = next((r for r in D[d0] if r["hm"] >= "09:30"), r0)
        st = w.end_straddle(r930)
        med = sorted(hist)[len(hist) // 2] if len(hist) >= 5 else None
        hist = (hist + [st])[-10:]
        vol = None if med is None else ("HIGH" if st > 1.2 * med else ("LOW" if st < 0.8 * med else "MID"))
        prev = D[days[i - 4]]
        chg = 100 * (r0["spot"] - prev[-1]["spot"]) / prev[-1]["spot"]
        trend = 0 if abs(chg) <= 1.5 else (1 if chg > 0 else -1)
        if cfg.get("skip_vol") and vol == cfg["skip_vol"]:
            continue
        if cfg.get("skip_trend") and trend:
            continue
        dc = dp = cfg.get("delta", 0.10)
        if cfg.get("wide_high_vol") and vol == "HIGH":
            dc = dp = 0.07
        sides = ["C", "P"]
        if trend and cfg.get("trend") == "skew":
            if trend > 0:
                dc = 0.05
            else:
                dp = 0.05
        if trend and cfg.get("trend") == "one_side":
            sides = ["P"] if trend > 0 else ["C"]
        legs = []
        for kind in sides:
            K = strike_for_delta(r0, kind, dc if kind == "C" else dp)
            legs.append([kind, K, 1])
            if cfg.get("wings"):
                legs.append([kind, K + cfg["wings"] if kind == "C" else K - cfg["wings"], -1])
        base_ch, base_sl = (250.0, 6.0) if cfg.get("wings") else (150.0, 4.0)
        credit = w.value([tuple(x) for x in legs], r0)
        realized = 0.0; extra_ch = 0.0; adj = 0
        spot0 = r0["spot"]
        for d in days[days.index(d0):i + 1]:
            for r in D[d]:
                if d == d0 and r["hm"] <= r0["hm"]:
                    continue
                if d == e and r["hm"] >= "15:00":
                    break
                if not cfg.get("adjust") or adj >= 2:
                    continue
                for kind in ("C", "P"):
                    shorts = [x for x in legs if x[0] == kind and x[2] == 1]
                    if not shorts:
                        continue
                    K = shorts[0][1]
                    trig = K if cfg.get("at") == "strike" else spot0 + 0.5 * (K - spot0)
                    hit = r["spot"] >= trig if kind == "C" else r["spot"] <= trig
                    if not hit:
                        continue
                    side_legs = [x for x in legs if x[0] == kind]
                    cost_close = w.value([tuple(x) for x in side_legs], r)      # buy back short, sell back wing
                    realized -= cost_close
                    extra_ch += 75 * len(side_legs); realized -= 2 * len(side_legs)
                    legs = [x for x in legs if x[0] != kind]
                    adj += 1
                    if cfg["adjust"] == "roll":
                        K2 = strike_for_delta(r, kind, 0.10)
                        new = [[kind, K2, 1]]
                        if cfg.get("wings"):
                            new.append([kind, K2 + cfg["wings"] if kind == "C" else K2 - cfg["wings"], -1])
                        realized += w.value([tuple(x) for x in new], r)
                        extra_ch += 75 * len(new); realized -= 2 * len(new)
                        legs += new
                    spot0 = r["spot"]
                    break
        settle = w.settle([tuple(x) for x in legs], D[e]) if legs else 0.0
        pts = credit + realized - settle - base_sl
        out.append(dict(day=d0, expiry=e, pts=pts, net=pts * LOT - base_ch - extra_ch, adj=adj, vol=vol, trend=trend))
    return out


def report(name, o):
    if not o:
        print("%-60s no trades" % name); return
    n = [x["net"] for x in o]; half = o[len(o) // 2]["day"]
    eq = pk = dd = 0.0
    for v in n:
        eq += v; pk = max(pk, eq); dd = min(dd, eq - pk)
    yr = collections.defaultdict(float)
    for x in o:
        yr[x["day"][:4]] += x["net"]
    print("%-60s n %3d win %5.1f%% net %+9.0f (H1 %+8.0f / H2 %+8.0f) worst %+8.0f maxDD %+8.0f adj %3d | %s" % (
        name, len(n), 100 * sum(v > 0 for v in n) / len(n), sum(n), sum(x["net"] for x in o if x["day"] < half),
        sum(x["net"] for x in o if x["day"] >= half), min(n), dd, sum(x["adj"] for x in o),
        " ".join("%s:%+.0f" % kv for kv in sorted(yr.items()))))


def main():
    D = {}
    for p in [a for a in sys.argv[1:] if not a.startswith("--")]:
        D.update(pickle.load(open(p, "rb")))
    w.EXP.clear(); w.EXP.update(w.expiries(D)); w.EXTRAP["mode"] = "linear"
    print("days %d (%s .. %s), expiry weeks %d; lot 20; 1 trading day before expiry, 09:30 entry" % (len(D), min(D), max(D), len(w.EXP)))
    variants = [
        ("strangle d0.10 (base)", {}),
        ("strangle d0.10 + d0.07 in HIGH vol", {"wide_high_vol": True}),
        ("condor d0.10 + 1000 wings (base)", {"wings": 1000}),
        ("condor + d0.07 in HIGH vol", {"wings": 1000, "wide_high_vol": True}),
    ]
    if "--retrace" in sys.argv:
        base = list(variants); variants = []
        entries = [("09:30", ("time", "09:30")), ("12:00", ("time", "12:00")), ("15:00", ("time", "15:00")),
                   ("pullback 0.3% (skip if none)", ("pullback", 0.3, "skip")), ("pullback 0.3% (else 15:00)", ("pullback", 0.3, "15:00")),
                   ("pullback 0.5% (skip if none)", ("pullback", 0.5, "skip")), ("pullback 0.5% (else 15:00)", ("pullback", 0.5, "15:00")),
                   ("straddle +10% spike (skip if none)", ("spike", 1.10, "skip")), ("straddle +10% spike (else 15:00)", ("spike", 1.10, "15:00"))]
        for bn, bc in base[1::2]:
            for en, ec in entries:
                variants.append(("%s | entry %s" % (bn, en), dict(bc, entry=ec)))
    for name, cfg in variants:
        report(name, simulate(D, cfg))


if __name__ == "__main__":
    main()
