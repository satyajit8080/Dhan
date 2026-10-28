#!/usr/bin/env python3
"""Check the 'Open Interest' Bullish/Bearish rule of VarunS2002/Python-NSE-Option-Chain-Analyzer on our logged SENSEX scans.

  python3 tools/test_oi_change_signal.py events.jsonl [events.jsonl ...]

Rule (that tool's set_values): call_sum = day OI change of calls at the chosen strike and the next 2 strikes up,
put_sum = the same for puts; Bearish when call_sum >= put_sum, else Bullish. Here the chosen strike is the scan's ATM
strike and only the logged ATM+-2 legs are available (so +1/+2 may be missing late in a trending day). Outcome = move
of the parity forward over the next 15/30/60 minutes, signed in the signal's direction. Local files only; no orders.
"""
import datetime, json, sys


def scans(path):
    out = []
    for line in open(path):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event") != "scan" or e.get("status") != "OK" or not e.get("forward"):
            continue
        legs = {(g["type"], g["strike"]): g for g in e["observation"]["legs"] if g.get("oiChange") is not None}
        ks = [k for k in sorted({k for _, k in legs}) if k >= e["atmStrike"]][:3]
        if len(ks) < 2:
            continue
        cs = sum(legs[("CE", k)]["oiChange"] for k in ks if ("CE", k) in legs)
        ps = sum(legs[("PE", k)]["oiChange"] for k in ks if ("PE", k) in legs)
        out.append((datetime.datetime.fromisoformat(e["startedIst"]), e["forward"], "Bull" if ps > cs else "Bear"))
    return out


def main():
    res = {15: [], 30: [], 60: []}
    for path in sys.argv[1:]:
        sc = scans(path)
        if not sc:
            continue
        print("%s scans %d Bullish %.0f%% day move %+.0f" % (sc[0][0].date(), len(sc),
              100 * sum(s == "Bull" for *_, s in sc) / len(sc), sc[-1][1] - sc[0][1]))
        for i, (t, F, sig) in enumerate(sc):
            for h in res:
                j = next((j for j in range(i, len(sc)) if (sc[j][0] - t).total_seconds() >= h * 60), None)
                if j is not None:
                    res[h].append((sc[j][1] - F) * (1 if sig == "Bull" else -1))
    for h, r in res.items():
        print("horizon %2d min: n %d, signal right %.1f%%, avg pts in signal direction %+.2f"
              % (h, len(r), 100 * sum(m > 0 for m in r) / len(r), sum(r) / len(r)))


if __name__ == "__main__":
    main()
