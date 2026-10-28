#!/usr/bin/env python3
"""Does HOW SENSEX is moving (broad vs narrow, which sectors) predict where it goes next? Local files only; no orders.

  python3 tools/test_breadth_sectors.py sensex_1min.csv STOCK_DATA_DIR

At snapshot times (10:00, 10:30, 11:00, 12:00, 12:30, 13:30) each day, using the 30 SENSEX stocks' NSE prices
(spot field of the rolling stock-option files) and approximate SENSEX weights:
  sx_chg       SENSEX % change vs previous close
  breadth      share of stocks above their previous close (0..1)
  sector       weighted contribution (%) of IT, banks, finance, energy, others
  top3         share of the absolute weighted move explained by the 3 biggest contributors (narrow vs broad)
Rules (UP / DOWN / no trade), each also tested in reverse (fade):
  broad        SENSEX change > X and breadth agrees (>= 0.7 up or <= 0.3 down) -> follow
  narrow       |SENSEX change| > X but top3 >= 0.6 and breadth does not agree -> fade
  it_vs_banks  IT and banks contributions have opposite signs -> follow banks (and the reverse: follow IT)
  sector_lead  the sector with the biggest absolute contribution -> follow its sign
Targets: SENSEX move over the next 30 / 60 min and to 15:25, points. Halves split at 28 Apr 2026; costs to beat:
~22 pts per future round trip, ~5 pts per ATM option round trip (option P&L ~ 0.5 x index move minus decay, so an
index move of ~10+ pts per trade is needed before decay).
"""
import csv, datetime, glob, json, os, sys
from collections import defaultdict

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
W = {"HDFCBANK": 15, "ICICIBANK": 10, "RELIANCE": 10, "INFY": 6, "BHARTIARTL": 5, "LT": 4.5, "ITC": 4, "TCS": 4,
     "AXISBANK": 3.5, "KOTAKBANK": 3.5, "SBIN": 3, "M&M": 3, "BAJFINANCE": 2.5, "HINDUNILVR": 2.5, "SUNPHARMA": 2,
     "HCLTECH": 2, "MARUTI": 1.8, "NTPC": 1.7, "ULTRACEMCO": 1.5, "TITAN": 1.5, "TMPV": 1.2, "POWERGRID": 1.3,
     "BAJAJFINSV": 1.2, "TATASTEEL": 1.2, "ASIANPAINT": 1.1, "ADANIPORTS": 1.1, "TECHM": 1.0, "ETERNAL": 1.2,
     "TRENT": 1.0, "BEL": 1.0}
SECTOR = {"IT": ("INFY", "TCS", "HCLTECH", "TECHM"), "BANKS": ("HDFCBANK", "ICICIBANK", "AXISBANK", "KOTAKBANK", "SBIN"),
          "FIN": ("BAJFINANCE", "BAJAJFINSV"), "ENERGY": ("RELIANCE", "NTPC", "POWERGRID")}
SNAPS = ("10:00", "10:30", "11:00", "12:00", "12:30", "13:30")


def sector_of(n):
    return next((s for s, ns in SECTOR.items() if n in ns), "OTHER")


def load(csvfile, sdir):
    sx = defaultdict(dict)
    for r in csv.DictReader(open(csvfile)):
        dt = datetime.datetime.fromtimestamp(int(r["Epoch"]), IST)
        sx[dt.date().isoformat()][dt.strftime("%H:%M")] = float(r["Close"])
    st = defaultdict(lambda: defaultdict(dict))               # name -> day -> hm -> spot
    for f in glob.glob(os.path.join(sdir, "*_CALL_*.json")):
        name = os.path.basename(f).rsplit("_", 2)[0]
        p = json.load(open(f)); leg = p.get("ce") if isinstance(p, dict) else None
        if not leg or not leg.get("timestamp"):
            continue
        for t, s in zip(leg["timestamp"], leg["spot"]):
            dt = datetime.datetime.fromtimestamp(t, IST)
            st[name][dt.date().isoformat()][dt.strftime("%H:%M")] = s
    return sx, st


def at(m, hm):
    """value at hm or the last one before it"""
    ks = [k for k in m if k <= hm]
    return m[max(ks)] if ks else None


def main():
    sx, st = load(sys.argv[1], sys.argv[2])
    names = [n for n in W if n in st]
    days = sorted(sx); half = days[len(days) // 2]
    print("stocks with data: %d / 30; days %d; halves split at %s" % (len(names), len(days), half))
    rows = []
    for i, d in enumerate(days[1:], 1):
        pd_ = days[i - 1]
        pcs = at(sx[pd_], "15:30")
        if pcs is None:
            continue
        for T in SNAPS:
            s0 = at(sx[d], T)
            if s0 is None:
                continue
            contrib = {}; up = n = 0
            for nm in names:
                pc = at(st[nm].get(pd_, {}), "15:30"); now = at(st[nm].get(d, {}), T)
                if not pc or not now:
                    continue
                ch = 100 * (now - pc) / pc
                contrib[nm] = W[nm] / 100 * ch; n += 1; up += ch > 0
            if n < 20:
                continue
            sec = defaultdict(float)
            for nm, c in contrib.items():
                sec[sector_of(nm)] += c
            tot = sum(abs(c) for c in contrib.values()) or 1e-9
            top3 = sum(sorted((abs(c) for c in contrib.values()), reverse=True)[:3]) / tot
            hh, mm = map(int, T.split(":"))
            fut = {}
            for H in (30, 60):
                t2 = "%02d:%02d" % divmod(hh * 60 + mm + H, 60)
                v = at(sx[d], t2) if t2 <= "15:25" else None
                if v is not None:
                    fut[H] = v - s0
            v = at(sx[d], "15:25")
            if v is not None:
                fut["close"] = v - s0
            rows.append(dict(day=d, T=T, chg=100 * (s0 - pcs) / pcs, breadth=up / n, sec=dict(sec), top3=top3, fut=fut))
    print("snapshots: %d" % len(rows))

    def sg(x):
        return 1 if x > 0 else (-1 if x < 0 else 0)

    rules = {}
    for X in (0.3, 0.6):
        rules["broad move > %.1f%% -> follow" % X] = (lambda X: lambda r: sg(r["chg"]) if abs(r["chg"]) > X and (
            (r["chg"] > 0 and r["breadth"] >= 0.7) or (r["chg"] < 0 and r["breadth"] <= 0.3)) else 0)(X)
        rules["narrow move > %.1f%% -> fade" % X] = (lambda X: lambda r: -sg(r["chg"]) if abs(r["chg"]) > X and r["top3"] >= 0.6
                                                    and not ((r["chg"] > 0 and r["breadth"] >= 0.6) or (r["chg"] < 0 and r["breadth"] <= 0.4)) else 0)(X)
    rules["IT vs banks opposite -> follow banks"] = lambda r: sg(r["sec"].get("BANKS", 0)) if r["sec"].get("IT", 0) * r["sec"].get("BANKS", 0) < 0 else 0
    rules["IT vs banks opposite -> follow IT"] = lambda r: sg(r["sec"].get("IT", 0)) if r["sec"].get("IT", 0) * r["sec"].get("BANKS", 0) < 0 else 0
    rules["biggest sector -> follow"] = lambda r: sg(max(r["sec"].values(), key=abs)) if r["sec"] else 0
    rules["breadth >= 0.7 up / <= 0.3 down -> follow"] = lambda r: 1 if r["breadth"] >= 0.7 else (-1 if r["breadth"] <= 0.3 else 0)
    rules["breadth disagrees with SENSEX -> follow breadth"] = lambda r: (1 if r["breadth"] > 0.5 else -1) if (r["chg"] > 0) != (r["breadth"] > 0.5) and abs(r["breadth"] - 0.5) >= 0.1 else 0
    import random

    def search(flip):
        res = []
        for rn, rule in rules.items():
            for mode, m in (("", 1), (" [REVERSED]", -1)):
                for T in SNAPS:
                    for H in (30, 60, "close"):
                        h = [[], []]
                        for r in rows:
                            if r["T"] != T or H not in r["fut"]:
                                continue
                            s = rule(r) * m
                            if s:
                                h[r["day"] >= half].append(s * r["fut"][H] * (-1 if flip and flip[r["day"]] else 1))
                        if len(h[0]) >= 15 and len(h[1]) >= 15:
                            res.append((sum(h[0]) / len(h[0]), sum(h[1]) / len(h[1])))
        return res
    random.seed(11)
    base = []
    for _ in range(5):
        b = search({d: random.random() < 0.5 for d in days})
        base.append(sum(x > 22 and y > 22 for x, y in b))
    print("chance baseline (each day's outcome randomly sign-flipped, 5 runs): combinations > 22 pts in both halves: %s"
          % base)
    out = []
    for rn, rule in rules.items():
        for mode, m in (("", 1), (" [REVERSED]", -1)):
            for T in SNAPS:
                for H in (30, 60, "close"):
                    h = [[], []]
                    for r in rows:
                        if r["T"] != T or H not in r["fut"]:
                            continue
                        s = rule(r) * m
                        if s:
                            h[r["day"] >= half].append(s * r["fut"][H])
                    if len(h[0]) >= 15 and len(h[1]) >= 15:
                        a0, a1 = sum(h[0]) / len(h[0]), sum(h[1]) / len(h[1])
                        out.append((a0, a1, rn + mode, T, H, len(h[0]), len(h[1]),
                                    100 * sum(x > 0 for x in h[0] + h[1]) / len(h[0] + h[1])))
    print("rule x time x horizon combinations (>= 15 trades per half): %d" % len(out))
    print("avg pts > 0 in both halves: %d | > 10 pts (option-viable) in both: %d | > 22 pts (futures-viable) in both: %d"
          % (sum(o[0] > 0 and o[1] > 0 for o in out), sum(o[0] > 10 and o[1] > 10 for o in out),
             sum(o[0] > 22 and o[1] > 22 for o in out)))
    fmt = "  %-48s %s hold %-5s | n %3d/%3d win %4.1f%% | avg SENSEX pts in trade direction H1 %+6.1f / H2 %+6.1f"
    print("\nTop 12 chosen on the FIRST half -> second half:")
    for o in sorted(out, key=lambda o: -o[0])[:12]:
        print(fmt % (o[2], o[3], o[4], o[5], o[6], o[7], o[0], o[1]))
    print("\nBest by the weaker half:")
    for o in sorted(out, key=lambda o: -min(o[0], o[1]))[:12]:
        print(fmt % (o[2], o[3], o[4], o[5], o[6], o[7], o[0], o[1]))


if __name__ == "__main__":
    main()
