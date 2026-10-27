#!/usr/bin/env python3
"""Iron fly / short strangle back-test on REAL expired SENSEX weekly options (Dhan rolling strikes, expiry number 1).

  python3 tools/backtest_wings.py ATM_HISTORY_DIR WINGS_HISTORY_DIR

Inputs are decoded history folders: rolling ATM CALL/PUT (history_options) and rolling ATM-10/ATM+10 (history_wings,
exactly 1,000 pts from the ATM strike). A held strike is priced each minute from three real points of the smile:
ATM (straddle), +1,000 call and -1,000 put. Each gives a Bachelier sigma*sqrt(T); the held strike's value uses the
linear interpolation by distance from the forward F = K + CE - PE (flat beyond +-1,000). At entry the held strikes
are exactly the observed ones, so the entry credit is the real premium.
Local files only, no orders, no API calls.
"""
import collections, datetime, glob, json, math, sys

phi = lambda x: math.exp(-x * x / 2) / math.sqrt(2 * math.pi)
Phi = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
LOT = 20


def call_px(F, K, s):
    d = (F - K) / s
    return (F - K) * Phi(d) + s * phi(d)


def put_px(F, K, s):
    return call_px(F, K, s) - (F - K)


def solve(f, target, hi):
    lo = 1e-4
    hi = max(hi, 1.0)
    for _ in range(50):
        m = (lo + hi) / 2
        if f(m) > target:
            hi = m
        else:
            lo = m
    return (lo + hi) / 2


def leg(path, side):
    try:
        l = json.load(open(path))["payload"].get(side) or {}
    except (OSError, ValueError):
        return None
    return l if l.get("timestamp") else None


def load(adir, wdir):
    """{day: [row]} with row = hm, K, F, sA, cK, sC, pK, sP (sigma*sqrt(T) at ATM, call wing, put wing)."""
    D = {}
    for f in sorted(glob.glob(adir + "/OPT_1_ATM_CALL_*.json")):
        d = f.rsplit("_", 1)[1][:-5]
        ce, pe = leg(f, "ce"), leg(f.replace("CALL", "PUT"), "pe")
        wc = leg("%s/OPT_1_ATM+10_CALL_%s_%s.json" % (wdir, d, d), "ce")
        wp = leg("%s/OPT_1_ATM-10_PUT_%s_%s.json" % (wdir, d, d), "pe")
        if not (ce and pe and wc and wp):
            continue
        ix = [{t: i for i, t in enumerate(l["timestamp"])} for l in (pe, wc, wp)]
        rows = []
        for i, t in enumerate(ce["timestamp"]):
            if not all(t in m for m in ix):
                continue
            j, a, b = (m[t] for m in ix)
            K = ce["strike"][i]
            if pe["strike"][j] != K:
                continue
            c0, p0 = ce["close"][i], pe["close"][j]
            F = K + c0 - p0
            st = c0 + p0
            sA = solve(lambda s: call_px(F, K, s) + put_px(F, K, s), st, 3 * st)
            cK, c = wc["strike"][a], wc["close"][a]
            pK, p = wp["strike"][b], wp["close"][b]
            sC = solve(lambda s: call_px(F, cK, s), max(c, call_px(F, cK, 1e-4) + 0.01), 3 * st)
            sP = solve(lambda s: put_px(F, pK, s), max(p, put_px(F, pK, 1e-4) + 0.01), 3 * st)
            hm = datetime.datetime.fromtimestamp(t + 19800, datetime.timezone.utc).strftime("%H:%M")
            rows.append(dict(hm=hm, K=K, F=F, sA=sA, cK=cK, c=c, sC=sC, pK=pK, p=p, sP=sP, spot=ce["spot"][i]))
        if rows:
            D[d] = rows
    return D


def sig(r, K):
    m = K - r["F"]
    if m >= 0:
        w = min(1.0, m / max(1.0, r["cK"] - r["F"]))
        return r["sA"] + w * (r["sC"] - r["sA"])
    w = min(1.0, -m / max(1.0, r["F"] - r["pK"]))
    return r["sA"] + w * (r["sP"] - r["sA"])


def value(pos, r):
    """Value (pts) of a short position: list of (kind, strike, qty) with qty +1 sold, -1 bought."""
    v = 0.0
    for kind, K, q in pos:
        s = max(sig(r, K), 1e-4)
        v += q * (call_px(r["F"], K, s) if kind == "C" else put_px(r["F"], K, s))
    return v


def expiries(D):
    """Expiry days: Thursday, or the last trading day before it when Thursday is a holiday."""
    wk = collections.defaultdict(list)
    for d in sorted(D):
        x = datetime.date.fromisoformat(d)
        if x.weekday() <= 3:
            wk[x.isocalendar()[:2]].append(d)
    return sorted(v[-1] for v in wk.values())


def end_straddle(r):
    return call_px(r["F"], r["K"], r["sA"]) + put_px(r["F"], r["K"], r["sA"])


def settle(pos, R):
    """Expiry settlement: intrinsic value at the average spot of the last 30 minutes."""
    xs = [r["spot"] for r in R if r["hm"] >= "15:00"] or [R[-1]["spot"]]
    S = sum(xs) / len(xs)
    return sum(q * (max(0.0, S - K) if kind == "C" else max(0.0, K - S)) for kind, K, q in pos)


STRUCT = {  # name: (legs at entry from row r, charges Rs, slippage pts)
    "STRANGLE_1000": (lambda r: [("C", r["cK"], 1), ("P", r["pK"], 1)], 150.0, 4.0),
    "IRON_FLY_1000": (lambda r: [("C", r["K"], 1), ("P", r["K"], 1), ("C", r["cK"], -1), ("P", r["pK"], -1)], 250.0, 8.0),
}


def run(D, struct, entry_day, entry="09:30", exit_=None, stop=None, tp=None):
    """One trade per expiry week. entry_day(expiry, days) -> entry date or None. exit_ None = hold to expiry
    settlement; otherwise close at that time on the expiry day. stop/tp as fractions of the credit."""
    legs, charges, slip = STRUCT[struct]
    days = sorted(D)
    out = []
    for e in expiries(D):
        if e == days[-1] and datetime.date.fromisoformat(e).weekday() != 3:
            continue                                        # current, unfinished week
        d0 = entry_day(e, days)
        if d0 is None:
            continue
        r0 = next((r for r in D[d0] if r["hm"] >= entry), None)
        if r0 is None:
            continue
        pos = legs(r0)
        credit = value(pos, r0)
        pnl, why = None, "EXPIRY"
        for d in days[days.index(d0):days.index(e) + 1]:
            for r in D[d]:
                if d == d0 and r["hm"] <= r0["hm"]:
                    continue
                if d == e and exit_ and r["hm"] >= exit_:
                    pnl, why = credit - value(pos, r) - slip, "TIME"
                    break
                if d == e and r["hm"] >= "15:00" and not exit_:
                    break
                v = value(pos, r)
                if stop and v >= credit * (1 + stop):
                    pnl, why = credit - v - slip - 4, "SL"
                    break
                if tp and v <= credit * (1 - tp):
                    pnl, why = credit - v - slip, "TP"
                    break
            if pnl is not None:
                break
        if pnl is None:
            pnl = credit - settle(pos, D[e]) - slip / 2              # expiring legs need no closing trade
        out.append(dict(day=d0, expiry=e, credit=credit, pts=pnl, why=why, net=pnl * LOT - charges))
    return out


def report(name, o, lots=1):
    if not o:
        return
    half = o[len(o) // 2]["day"]
    h1 = sum(x["net"] for x in o if x["day"] < half); h2 = sum(x["net"] for x in o if x["day"] >= half)
    eq = pk = dd = 0.0
    for x in o:
        eq += x["net"]; pk = max(pk, eq); dd = min(dd, eq - pk)
    w = sum(x["pts"] > 0 for x in o)
    print("%-52s n %3d win %5.1f%% pts %+7.0f avg credit %4.0f worst %+6.0f | net 1 lot Rs %+8.0f (H1 %+7.0f / H2 %+7.0f) "
          "maxDD %+7.0f | %s" % (name, len(o), 100 * w / len(o), sum(x["pts"] for x in o), sum(x["credit"] for x in o) / len(o),
                                 min(x["pts"] for x in o), h1 + h2, h1, h2, dd, dict(collections.Counter(x["why"] for x in o))))


def intraday(D, struct, dayf, entry="09:30", exit_="15:00", stop=0.25, tp=0.30):
    """Same-day trade every chosen weekday: the report's iron fly rules (stop at credit +25%, take profit 30%)."""
    legs, charges, slip = STRUCT[struct]
    out = []
    for d in sorted(D):
        if not dayf(datetime.date.fromisoformat(d).weekday()):
            continue
        R = D[d]
        r0 = next((r for r in R if r["hm"] >= entry), None)
        if r0 is None:
            continue
        pos = legs(r0); credit = value(pos, r0); pnl, why = None, "TIME"
        for r in R:
            if r["hm"] <= r0["hm"]:
                continue
            v = value(pos, r)
            if r["hm"] >= exit_:
                pnl = credit - v - slip; break
            if stop and v >= credit * (1 + stop):
                pnl, why = credit - v - slip - 4, "SL"; break
            if tp and v <= credit * (1 - tp):
                pnl, why = credit - v - slip, "TP"; break
        if pnl is None:
            pnl = credit - value(pos, R[-1]) - slip
        out.append(dict(day=d, expiry=d, credit=credit, pts=pnl, why=why, net=pnl * LOT - charges))
    return out


def prev_day(n):
    """Entry n trading days before expiry (n=0: expiry morning)."""
    def f(e, days):
        i = days.index(e) - n
        return days[i] if i >= 0 and (n == 0 or days[i] not in EXP) else None
    return f


def after_last_expiry(e, days):
    ex = sorted(x for x in EXP if x < e)
    if not ex:
        return None
    i = days.index(ex[-1]) + 1
    return days[i] if days[i] <= e and days[i] != e else None


EXP = set()


def main():
    D = load(sys.argv[1], sys.argv[2])
    EXP.update(expiries(D))
    days = sorted(D)
    print("days %d (%s .. %s), expiry weeks %d. Costs: strangle Rs 150 + 4 pts, iron fly Rs 250 + 8 pts (+4 on stops); "
          "lot 20; expiry legs settle at the last-30-min average spot." % (len(D), days[0], days[-1], len(EXP)))
    names = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri"}
    print("-- intraday iron fly, 09:30-15:00, stop +25%, take profit 30% (research report)")
    for wd in range(5):
        report("IRON_FLY intraday %s" % names[wd], intraday(D, "IRON_FLY_1000", lambda w, wd=wd: w == wd))
    report("IRON_FLY intraday Mon/Tue/Wed/Fri", intraday(D, "IRON_FLY_1000", lambda w: w != 3))
    report("IRON_FLY intraday expiry day only", [x for x in intraday(D, "IRON_FLY_1000", lambda w: True) if x["day"] in EXP])
    print("-- held to expiry (one trade per week)")
    for struct in ("STRANGLE_1000", "IRON_FLY_1000"):
        for lbl, ef in (("day after last expiry", after_last_expiry), ("2 days before expiry", prev_day(2)),
                        ("1 day before expiry", prev_day(1)), ("expiry morning", prev_day(0))):
            for st in (None, 0.5, 1.0):
                for tp in (None, 0.5):
                    report("%s %s stop %s tp %s" % (struct, lbl, "-" if st is None else "%d%%" % (st * 100),
                                                     "-" if tp is None else "%d%%" % (tp * 100)),
                           run(D, struct, ef, stop=st, tp=tp))


if __name__ == "__main__":
    main()
