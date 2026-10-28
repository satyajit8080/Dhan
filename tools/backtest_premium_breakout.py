#!/usr/bin/env python3
"""Buy the ATM CE / PE when its OWN premium starts moving suddenly (premium breakout), real expired 1-min premiums.

  python3 tools/backtest_premium_breakout.py HISTORY_DIR

Signal on the rolling ATM CE or PE (nearest weekly), same strike for the whole lookback (rolling strike switches are
not moves): the premium rose by at least X % over the last N minutes (N = 1, 2, 3, 5; X = 3 / 5 / 8 / 12 %), and/or
closed above its highest close of the previous 15 minutes (option breakout). One trade at a time per side, no entry
in the no-trade windows used before (09:15-09:25, 11:30-13:00, after 14:50). Entry at the signal minute's close.
Exits: target +5 / +10 / +20 %, stop -5 / -10 %, time stop 5 / 10 / 20 min (stop first when a bar hits both). Strike
switches during the hold are bridged with delta 0.5. Costs: 1.5 pts slippage + Rs 70 per trade, lot 20.
Selection on the first half (before 28 Apr 2026), shown on the second; chance baseline = the same search with each
trade's direction... not applicable for buying, so the baseline is a random-entry version with the same trade count.
Local files only; no orders.
"""
import datetime, itertools, random, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest_real_options import load_options              # noqa: E402
from backtest_v3_rules import expiry_days                   # noqa: E402

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
LOT, CH, SLIP = 20, 70.0, 1.5


def no_trade(hm):
    return hm < "09:25" or "11:30" <= hm < "13:00" or hm >= "14:50"


def series(opt):
    out = {}
    for (d, side), leg in opt.items():
        ts = sorted(leg)
        out[(d, side)] = [(t, datetime.datetime.fromtimestamp(t, IST).strftime("%H:%M")) + leg[t] for t in ts]
    return out                                              # (t, hm, strike, o, h, l, c)


def exit_pnl(s, i, tp, sl, hold):
    K = s[i][2]; e = s[i][6]; cs = 1 if s[0] is None else 0
    last = e
    for k in range(i + 1, min(len(s), i + hold + 1)):
        t, hm, k2, o, h, l, c = s[k]
        sh = 0.0 if k2 == K else 0.5 * (k2 - K) * SIGN
        h, l, c = h + sh, l + sh, c + sh
        if l <= e * (1 - sl):
            return -e * sl - SLIP
        if h >= e * (1 + tp):
            return e * tp - SLIP
        last = c
    return last - e - SLIP


SIGN = 1


def signals(s, N, X, brk):
    out = []; busy = -1
    for i in range(max(N, 15), len(s)):
        if i <= busy or no_trade(s[i][1]):
            continue
        if any(s[j][2] != s[i][2] for j in range(i - max(N, 15), i + 1)):
            continue
        c, c0 = s[i][6], s[i - N][6]
        if c0 <= 0:
            continue
        ok = (X is None or (c - c0) / c0 >= X) and (not brk or c > max(s[j][6] for j in range(i - 15, i)))
        if ok:
            out.append(i); busy = i + 5
    return out


def main():
    global SIGN
    opt = load_options(sys.argv[1], 1); S = series(opt)
    exp = expiry_days(opt); days = sorted({d for d, _ in S}); half = days[len(days) // 2]
    trig = [(N, X, brk) for N in (1, 2, 3, 5) for X in (0.03, 0.05, 0.08, 0.12) for brk in (False, True)]
    trig += [(15, None, True)]                               # pure 15-min premium-high breakout
    exits = list(itertools.product((0.05, 0.10, 0.20), (0.05, 0.10), (5, 10, 20)))
    res = []
    for (N, X, brk) in trig:
        sig = {}
        for (d, side), s in S.items():
            sig[(d, side)] = signals(s, N, X, brk)
        for dayf_name, dayf in (("all days", lambda d: True), ("expiry day", lambda d: d in exp),
                                ("non-expiry", lambda d: d not in exp)):
            for tp, sl, hold in exits:
                h = [[], []]
                for (d, side), idx in sig.items():
                    if not dayf(d):
                        continue
                    SIGN = 1 if side == "CE" else -1
                    for i in idx:
                        h[d >= half].append(exit_pnl(S[(d, side)], i, tp, sl, hold))
                if len(h[0]) < 30 or len(h[1]) < 30:
                    continue
                net = [sum(x) * LOT - CH * len(x) for x in h]
                a = h[0] + h[1]
                res.append((net[0], net[1], N, X, brk, dayf_name, tp, sl, hold, len(h[0]), len(h[1]),
                            100 * sum(p > 0 for p in a) / len(a), sum(a) / len(a)))
    print("premium-breakout combinations (>= 30 trades per half): %d" % len(res))
    print("positive after costs: H1 %d, H2 %d, BOTH %d" % (sum(r[0] > 0 for r in res), sum(r[1] > 0 for r in res),
                                                            sum(r[0] > 0 and r[1] > 0 for r in res)))
    print("gross (before costs) average per trade over all combinations: %+.2f pts" % (sum(r[12] for r in res) / len(res) + SLIP))
    fmt = "N %2d X %-5s brk %-5s %-11s TP %3.0f%% SL %3.0f%% hold %2d | n %4d/%4d win %4.1f%% avg %+5.2f | net Rs H1 %+8.0f / H2 %+8.0f"
    print("\nTop 12 chosen on the FIRST half -> second half:")
    for r in sorted(res, key=lambda r: -r[0])[:12]:
        print(fmt % (r[2], r[3], r[4], r[5], 100 * r[6], 100 * r[7], r[8], r[9], r[10], r[11], r[12], r[0], r[1]))
    both = sorted([r for r in res if r[0] > 0 and r[1] > 0], key=lambda r: -min(r[0], r[1]))
    print("\nPositive in both halves (best 12):")
    for r in both[:12]:
        print(fmt % (r[2], r[3], r[4], r[5], 100 * r[6], 100 * r[7], r[8], r[9], r[10], r[11], r[12], r[0], r[1]))
    # follow-through: what the premium does in the next 5 / 10 min after a sudden jump (no exits, no costs)
    print("\nFollow-through after a sudden jump (premium change after entry, % of entry, no costs):")
    for N, X in ((1, 0.05), (2, 0.08), (3, 0.12), (5, 0.12)):
        for H in (1, 3, 5, 10):
            v = []
            for (d, side), s in S.items():
                for i in signals(s, N, X, False):
                    if i + H < len(s) and s[i + H][2] == s[i][2]:
                        v.append((s[i + H][6] - s[i][6]) / s[i][6] * 100)
            if v:
                print("  jump >= %2.0f%% in %d min -> after %2d min: avg %+.2f%%, up in %.0f%% of %d cases"
                      % (100 * X, N, H, sum(v) / len(v), 100 * sum(x > 0 for x in v) / len(v), len(v)))


if __name__ == "__main__":
    main()
