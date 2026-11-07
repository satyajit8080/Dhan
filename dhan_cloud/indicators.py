"""Pure-Python indicators (no numpy). Must match main.py exactly (tests/test_cloud_parity.py).
bars: list of dicts with ts, open, high, low, close, volume. NaN = float('nan') during warm-up."""
import datetime as dt
import math

NAN = float("nan")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def ok(x):
    return isinstance(x, (int, float)) and math.isfinite(x)


def wilder(x, n):
    out = [NAN] * len(x)
    s = next((i for i, v in enumerate(x) if ok(v)), None)
    if s is None or s + n > len(x) or not all(ok(v) for v in x[s:s + n]):
        return out
    out[s + n - 1] = sum(x[s:s + n]) / n
    for i in range(s + n, len(x)):
        out[i] = (out[i - 1] * (n - 1) + x[i]) / n if ok(x[i]) else out[i - 1]
    return out


def true_range(b):
    tr = []
    for i, x in enumerate(b):
        if i == 0:
            tr.append(x["high"] - x["low"])
        else:
            pc = b[i - 1]["close"]
            tr.append(max(x["high"] - x["low"], abs(x["high"] - pc), abs(x["low"] - pc)))
    return tr


def vwap(b):
    out, pv, vv, day = [], 0.0, 0.0, None
    for x in b:
        d = dt.datetime.fromtimestamp(x["ts"], IST).date()
        if d != day:
            pv, vv, day = 0.0, 0.0, d
        p = (x["high"] + x["low"] + x["close"]) / 3.0
        v = x.get("volume") or 0.0
        pv += p * v
        vv += v
        out.append(pv / vv if vv > 0 else NAN)
    return out


def atr(b, n=14):
    return wilder(true_range(b), n)


def supertrend(b, n=10, m=3.0):
    a = wilder(true_range(b), n)
    line, d = [NAN] * len(b), [NAN] * len(b)
    fu = fl = None
    for i, x in enumerate(b):
        if not ok(a[i]):
            continue
        hl2 = (x["high"] + x["low"]) / 2
        bu, bl = hl2 + m * a[i], hl2 - m * a[i]
        if fu is None:
            fu, fl = bu, bl
            d[i] = 1.0 if x["close"] >= hl2 else -1.0
        else:
            pc = b[i - 1]["close"]
            fu = bu if (bu < fu or pc > fu) else fu
            fl = bl if (bl > fl or pc < fl) else fl
            if d[i - 1] < 0:
                d[i] = 1.0 if x["close"] > fu else -1.0
            else:
                d[i] = -1.0 if x["close"] < fl else 1.0
        line[i] = fl if d[i] > 0 else fu
    return line, d


def adx(b, n=14):
    tr = true_range(b)
    tr[0] = NAN
    pdm, ndm = [NAN], [NAN]
    for i in range(1, len(b)):
        up = b[i]["high"] - b[i - 1]["high"]
        dn = b[i - 1]["low"] - b[i]["low"]
        pdm.append(up if (up > dn and up > 0) else 0.0)
        ndm.append(dn if (dn > up and dn > 0) else 0.0)
    a, sp, sn = wilder(tr, n), wilder(pdm, n), wilder(ndm, n)
    dip, dim, dx = [], [], []
    for i in range(len(b)):
        if ok(a[i]) and ok(sp[i]) and a[i] > 0:
            p_, m_ = 100 * sp[i] / a[i], 100 * sn[i] / a[i]
            dip.append(p_)
            dim.append(m_)
            dx.append(100 * abs(p_ - m_) / (p_ + m_) if p_ + m_ > 0 else NAN)
        else:
            dip.append(NAN)
            dim.append(NAN)
            dx.append(NAN)
    return wilder(dx, n), dip, dim


def rsi(b, n=14):
    g, l_ = [NAN], [NAN]
    for i in range(1, len(b)):
        ch = b[i]["close"] - b[i - 1]["close"]
        g.append(max(ch, 0.0))
        l_.append(max(-ch, 0.0))
    ag, al = wilder(g, n), wilder(l_, n)
    out = []
    for x, y in zip(ag, al):
        if not ok(x):
            out.append(NAN)
        elif y == 0:
            out.append(100.0 if x > 0 else 50.0)
        else:
            out.append(100 - 100 / (1 + x / y))
    return out


def bollinger(b, n=20, k=2.0):
    c = [x["close"] for x in b]
    mid, up, lo, sd, bw = ([NAN] * len(c) for _ in range(5))
    for i in range(n - 1, len(c)):
        w = c[i - n + 1:i + 1]
        m = sum(w) / n
        s = math.sqrt(sum((v - m) ** 2 for v in w) / n)
        mid[i], sd[i], up[i], lo[i] = m, s, m + k * s, m - k * s
        bw[i] = (up[i] - lo[i]) / m
    return {"mid": mid, "upper": up, "lower": lo, "sd": sd, "bandwidth": bw}


def efficiency_ratio(b, n=10):
    c = [x["close"] for x in b]
    out = [NAN] * len(c)
    for i in range(n, len(c)):
        den = sum(abs(c[j] - c[j - 1]) for j in range(i - n + 1, i + 1))
        out[i] = abs(c[i] - c[i - n]) / den if den > 0 else 0.0
    return out


def zscore(b, n=20):
    bb = bollinger(b, n)
    return [((x["close"] - m) / s if s > 0 else 0.0) if ok(s) else NAN for x, m, s in zip(b, bb["mid"], bb["sd"])]


def pivots(h, l, c):
    p = (h + l + c) / 3
    return {"P": p, "R1": 2 * p - l, "S1": 2 * p - h, "R2": p + (h - l), "S2": p - (h - l), "PDH": h, "PDL": l, "PDC": c}


def compute(b):
    st, sd = supertrend(b, 10, 3.0)
    a, dp, dm = adx(b, 14)
    bb = bollinger(b, 20, 2.0)
    return {"close": [x["close"] for x in b], "vwap": vwap(b), "atr": atr(b, 14), "st": st, "st_dir": sd,
            "adx": a, "di_plus": dp, "di_minus": dm, "rsi": rsi(b, 14), "bb_mid": bb["mid"], "bb_up": bb["upper"],
            "bb_lo": bb["lower"], "bw": bb["bandwidth"], "er": efficiency_ratio(b, 10), "z": zscore(b, 20)}
