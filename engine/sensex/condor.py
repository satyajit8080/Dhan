"""
PAPER iron condor tracker (user, 28 Oct 2026: "Paper only"). NO orders: it only reads market data through the
same read-only client and logs what the condor would have been.

Rules (tools/backtest_strangle_multiyear.py, best 3.4-year result):
  - entry: the trading day before the nearest weekly expiry, at the 09:30 IST 1-min candle close;
  - sell the first call above / put below the index whose delta is <= CONDOR_DELTA (0.10);
  - buy protection WING_PTS (1,000) further out on each side; hold to expiry.

The entry is rebuilt from Dhan 1-min candles of the entry day (09:30 bar close of each option), so the same
strikes and prices come out on the entry day and again on expiry day: no file has to survive between runs.
Delta is Black-76 on those closes with the forward from put-call parity at the ATM strike (r = 0), so it can
differ slightly from the delta column in the Dhan app. Settlement: intrinsic value at the average index
1-min close from 15:00 to 15:29 IST of expiry day (an approximation of the exchange settlement price).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .pricing import b76_greeks, b76_iv, ist_to_epoch_ms, year_fraction_to_expiry
from .session import IST

INDEX_SCRIP, INDEX_SEG = "51", "IDX_I"
OPT_SEG, OPT_INSTRUMENT = "BSE_FNO", "OPTIDX"
CHARGES_PER_LOT_RS = 250.0          # back-test assumption for one 4-leg round trip of 1 lot
SLIPPAGE_PTS = 6.0                  # back-test assumption per trade (all four legs)


def prev_trading_day(d: date, holidays=frozenset()) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5 or d in holidays:
        d -= timedelta(days=1)
    return d


def plan(today: date, expiries, holidays=frozenset()) -> dict:
    """ENTRY on the trading day before the nearest expiry, SETTLE on expiry day, otherwise WAIT."""
    future = sorted(e for e in (date.fromisoformat(x) for x in expiries) if e >= today)
    if not future:
        return {"action": "NONE", "reason": "no expiry on or after today"}
    e = future[0]
    entry = prev_trading_day(e, holidays)
    if today == e:
        return {"action": "SETTLE", "expiry": e.isoformat(), "entryDay": entry.isoformat()}
    if today == entry:
        return {"action": "ENTRY", "expiry": e.isoformat(), "entryDay": entry.isoformat()}
    return {"action": "WAIT", "expiry": e.isoformat(), "entryDay": entry.isoformat()}


def chain_ids(chain_payload) -> dict:
    """{strike: {"CE": security_id, "PE": security_id}} from a raw /optionchain payload."""
    out = {}
    oc = (chain_payload or {}).get("oc") or {}
    for key, v in oc.items():
        try:
            k = float(key)
        except (TypeError, ValueError):
            continue
        row = {}
        for side in ("ce", "pe"):
            leg = (v or {}).get(side) or {}
            sid = leg.get("security_id")
            if sid not in (None, ""):
                row[side.upper()] = str(sid)
        if row:
            out[k] = row
    return out


def _bar_close(payload, day: str, hhmm: str):
    """Close of the 1-min bar starting at hhmm IST on day (or the last bar before it)."""
    p = payload if isinstance(payload, dict) else {}
    ts, cl = p.get("timestamp") or [], p.get("close") or []
    want = ist_to_epoch_ms("%sT%s" % (day, hhmm)) // 1000
    best = None
    for t, c in zip(ts, cl):
        if t is not None and c is not None and int(t) <= want and int(t) >= want - 900:
            best = float(c)
    return best


def _closes(payload, day: str, start: str, end: str) -> list:
    p = payload if isinstance(payload, dict) else {}
    a = ist_to_epoch_ms("%sT%s" % (day, start)) // 1000
    b = ist_to_epoch_ms("%sT%s" % (day, end)) // 1000
    return [float(c) for t, c in zip(p.get("timestamp") or [], p.get("close") or [])
            if t is not None and c is not None and a <= int(t) <= b]


class CondorPaper:
    """Read-only: uses client.option_chain / client.intraday_candles only."""

    def __init__(self, client, log, lots: int, lot_size: int, delta: float = 0.10, wing_pts: float = 1000.0,
                 entry_time: str = "09:30", max_steps: int = 40):
        self.client, self.log = client, log
        self.lots, self.lot_size = int(lots), int(lot_size)
        self.delta, self.wing, self.entry_time, self.max_steps = float(delta), float(wing_pts), entry_time, max_steps

    @staticmethod
    def plan_action(today: date, expiries, holidays=frozenset()) -> str:
        return plan(today, expiries, holidays)["action"]

    def _option_close(self, sid: str, day: str):
        r = self.client.intraday_candles(sid, OPT_SEG, OPT_INSTRUMENT, 1, day + " 09:15:00", day + " 09:45:00")
        return _bar_close(r.payload, day, self.entry_time)

    def rebuild_entry(self, expiry: str, entry_day: str, chain_payload) -> dict:
        """The 4 legs and their 09:30 closes on entry_day. Raises ValueError with a reason when it cannot."""
        ids = chain_ids(chain_payload)
        strikes = sorted(ids)
        if not strikes:
            raise ValueError("option chain has no strikes with security ids")
        r = self.client.intraday_candles(INDEX_SCRIP, INDEX_SEG, "INDEX", 1, entry_day + " 09:15:00",
                                         entry_day + " 09:45:00")
        spot = _bar_close(r.payload, entry_day, self.entry_time)
        if spot is None:
            raise ValueError("no index candle at %s on %s (holiday? add it to HOLIDAYS)" % (self.entry_time, entry_day))
        atm = min(strikes, key=lambda k: abs(k - spot))
        if "CE" not in ids[atm] or "PE" not in ids[atm]:
            raise ValueError("ATM strike %s lacks a call or put id" % atm)
        c0, p0 = self._option_close(ids[atm]["CE"], entry_day), self._option_close(ids[atm]["PE"], entry_day)
        if c0 is None or p0 is None:
            raise ValueError("no 09:30 option candle at the ATM strike on %s" % entry_day)
        F = atm + c0 - p0
        now_ms = ist_to_epoch_ms("%sT%s" % (entry_day, self.entry_time)) + 60_000
        T = year_fraction_to_expiry(now_ms, expiry)
        if not T > 0:
            raise ValueError("entry is not before expiry")
        prices = {}

        def pick(side):
            sign = 1 if side == "CE" else -1
            cands = [k for k in strikes if (k > atm if side == "CE" else k < atm)]
            cands.sort(key=lambda k: abs(k - atm))
            for k in cands[:self.max_steps]:
                if side not in ids[k]:
                    continue
                px = self._option_close(ids[k][side], entry_day)
                if px is None or px <= 0:
                    continue
                iv = b76_iv(px, F, k, T, 0.0, side)
                if iv is None or iv <= 0:
                    continue
                d = abs(b76_greeks(F, k, T, iv, 0.0, side)["delta"])
                if d <= self.delta:
                    prices[(k, side)] = px
                    return k, d
            raise ValueError("no %s strike with delta <= %.2f within %d strikes" % (side, self.delta, self.max_steps))

        kc, dc = pick("CE")
        kp, dp = pick("PE")
        legs = [("SELL", "CE", kc, dc), ("SELL", "PE", kp, dp), ("BUY", "CE", kc + self.wing, None),
                ("BUY", "PE", kp - self.wing, None)]
        out = []
        for act, side, k, d in legs:
            if (k, side) not in prices:
                if k not in ids or side not in ids[k]:
                    raise ValueError("protection strike %s %s is not in the chain" % (k, side))
                px = self._option_close(ids[k][side], entry_day)
                if px is None:
                    raise ValueError("no 09:30 candle for %s %s" % (k, side))
                prices[(k, side)] = px
            out.append({"action": act, "type": side, "strike": k, "price": round(prices[(k, side)], 2),
                        "delta": None if d is None else round(d, 4)})
        credit = sum(l["price"] * (1 if l["action"] == "SELL" else -1) for l in out)
        if credit <= 0:
            raise ValueError("net credit %.2f is not positive" % credit)
        q = self.lots * self.lot_size
        return {"expiry": expiry, "entryDay": entry_day, "entryTime": self.entry_time, "spot": spot,
                "forward": round(F, 2), "legs": out, "creditPts": round(credit, 2),
                "breakEvens": [round(kp - credit, 2), round(kc + credit, 2)],
                "lots": self.lots, "lotSize": self.lot_size, "quantityPerLeg": q,
                "maxProfitRs": round(credit * q), "maxLossRs": round((self.wing - credit) * q),
                "estChargesRs": round(CHARGES_PER_LOT_RS * self.lots)}

    def settle(self, entry: dict, day: str, final: bool) -> dict:
        r = self.client.intraday_candles(INDEX_SCRIP, INDEX_SEG, "INDEX", 1, day + " 09:15:00", day + " 15:30:00")
        xs = _closes(r.payload, day, "15:00", "15:29")
        if not xs:
            allc = _closes(r.payload, day, "09:15", "15:29")
            if not allc:
                raise ValueError("no index candles on %s" % day)
            xs, final = allc[-1:], False
        S = sum(xs) / len(xs)
        value = 0.0
        for l in entry["legs"]:
            intr = max(0.0, S - l["strike"]) if l["type"] == "CE" else max(0.0, l["strike"] - S)
            value += intr * (1 if l["action"] == "SELL" else -1)
        pts = entry["creditPts"] - value
        q = entry["quantityPerLeg"]
        net = (pts - SLIPPAGE_PTS) * q - CHARGES_PER_LOT_RS * entry["lots"]
        return {"status": "FINAL" if final else "PROVISIONAL", "settleSpot": round(S, 2), "spotBars": len(xs),
                "expiryValuePts": round(value, 2), "pnlPts": round(pts, 2), "grossRs": round(pts * q),
                "estNetRs": round(net), "assumptions": "settlement = average index close 15:00-15:29; net = gross - "
                "%g pts slippage - Rs %g per lot" % (SLIPPAGE_PTS, CHARGES_PER_LOT_RS)}

    def run(self, today: date, now_ist: datetime, expiries, holidays=frozenset()) -> dict:
        """One call per run. Logs condor_plan / condor_entry / condor_settle / condor_skip; never raises."""
        p = plan(today, expiries, holidays)
        self.log("info", "condor_plan", orders="NONE (paper)", **p)
        if p["action"] not in ("ENTRY", "SETTLE"):
            return p
        if p["action"] == "ENTRY" and now_ist.strftime("%H:%M") <= self.entry_time:
            self.log("info", "condor_skip", reason="before the %s candle closed; run again after %s" %
                     (self.entry_time, self.entry_time))
            return p
        try:
            chain = self.client.option_chain(p["expiry"])
            entry = self.rebuild_entry(p["expiry"], p["entryDay"], chain.payload)
            self.log("info", "condor_entry", paper=True, orders="NONE", **entry)
            if p["action"] == "SETTLE":
                res = self.settle(entry, today.isoformat(), final=now_ist.strftime("%H:%M") >= "15:30")
                self.log("info", "condor_settle", paper=True, orders="NONE", expiry=p["expiry"], **res)
                if res["status"] == "PROVISIONAL":
                    self.log("info", "condor_note", note="provisional (market still open): run again after 15:30 "
                                                         "IST for the final result")
                p["result"] = res
            p["entry"] = entry
        except Exception as e:      # paper tracking must never stop the observer
            self.log("warn", "condor_skip", reason="%s: %s" % (type(e).__name__, e))
        return p
