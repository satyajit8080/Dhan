"""
PAPER tracker for the user's exit rule: take profit +6 / stop loss -11 option
premium points (user, 9 Oct 2026), time stop 10 min (RULES.md §6).

OBSERVATION ONLY. Nothing here talks to a broker: it receives prices that the
read-only scanner already fetched and records what WOULD have happened. There
is no order, position or account logic anywhere in this module.

Entry event (exploratory, NOT a CE/PE signal; D1-D8 are undecided):
  the SENSEX index CROSSES a refresh-table trigger of a row whose status is OK
  (CE: from below to >= breakoutAbove; PE: from above to <= breakdownBelow),
  outside the no-trade windows. Each (side, trigger) fires once per session,
  for every configured strike of that side (one paper trade per strike).
Fill model: entry at the option's ASK at the first poll after the crossing;
  exits evaluated on the BID (what a sell would get). Both sides of the spread
  are therefore already inside the result. Polling is discrete (every few
  seconds), so a move that hits both levels between two polls cannot be
  ordered; such a case is impossible to see and is not invented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class PaperConfig:
    take_profit_pts: float = 6.0       # user rule, option premium points
    stop_loss_pts: float = 11.0        # user rule, option premium points
    time_stop_s: float = 600.0         # RULES.md §6 time stop 10 min
    max_open: int = 40
    lot_size: int | None = None        # set to report rupees; never guessed
    cost_per_trade_rs: float | None = None   # brokerage + statutory per round trip, if you supply it


@dataclass
class _Arm:
    side: str
    trigger: float
    strikes: list
    fired: bool = False


@dataclass
class _Trade:
    tid: str
    side: str
    strike: float
    security_id: str
    trigger: float
    entry_ms: int
    entry_ask: float
    entry_bid: float | None
    index_at_cross: float
    best: float = 0.0
    worst: float = 0.0
    ticks: int = 0
    gaps: int = 0
    last_bid: float | None = None
    path: list = field(default_factory=list)


class PaperTracker:
    def __init__(self, cfg: PaperConfig, log, in_skip_window):
        self.cfg, self.log, self.in_skip_window = cfg, log, in_skip_window
        self.arms: dict = {}             # (side, trigger) -> _Arm
        self.fired: set = set()          # (side, trigger) fired this session
        self.open: list = []
        self.closed: list = []
        self.last_index: float | None = None
        self.legs: dict = {}             # (strike, side) -> security id
        self.seq = 0

    # ------------------------------------------------------------ from scans
    def arm(self, rows, legs: dict, scan_ok: bool):
        """Replace the armed triggers with the latest scan's OK rows."""
        self.legs = dict(legs)
        self.arms = {}
        if not scan_ok or not rows:
            return
        for r in rows:
            if r.status != "OK" or r.trigger is None:
                continue
            key = (r.side, float(r.trigger))
            if key in self.fired:
                continue
            self.arms.setdefault(key, _Arm(r.side, float(r.trigger), []))
            if r.strike not in self.arms[key].strikes:
                self.arms[key].strikes.append(r.strike)

    def watch_ids(self) -> list:
        ids = {self.legs[k] for k in self.legs if self.legs[k]}
        ids |= {t.security_id for t in self.open}
        return sorted(ids)

    # ------------------------------------------------------------- per poll
    def on_tick(self, now_ms: int, index_ltp, quotes: dict):
        """quotes: security_id -> (bid, ask). Returns nothing; logs events."""
        for t in list(self.open):
            self._update(t, now_ms, quotes.get(t.security_id))
        prev, self.last_index = self.last_index, index_ltp
        if index_ltp is None or prev is None:
            return
        now = datetime.fromtimestamp(now_ms / 1000, timezone.utc)
        if self.in_skip_window(now):
            return
        for key, arm in list(self.arms.items()):
            crossed = (prev < arm.trigger <= index_ltp) if arm.side == "CE" else (prev > arm.trigger >= index_ltp)
            if not crossed:
                continue
            self.fired.add(key)
            del self.arms[key]
            for strike in arm.strikes:
                self._enter(arm, strike, now_ms, index_ltp, quotes)

    def _enter(self, arm, strike, now_ms, index_ltp, quotes):
        sid = self.legs.get((strike, arm.side))
        q = quotes.get(sid) if sid else None
        base = {"side": arm.side, "strike": strike, "trigger": arm.trigger, "indexAtCross": index_ltp}
        if q is None or not q[1] or q[1] <= 0:
            self.log("info", "paper_skip", reason="no ask quote for the leg at the crossing", **base)
            return
        if len(self.open) >= self.cfg.max_open:
            self.log("warn", "paper_skip", reason="max_open reached", **base)
            return
        self.seq += 1
        bid, ask = q
        t = _Trade("P%03d" % self.seq, arm.side, strike, sid, arm.trigger, now_ms, ask, bid, index_ltp)
        self.open.append(t)
        self.log("info", "paper_entry", paperId=t.tid, entryAsk=ask, entryBid=bid,
                 spreadPts=round(ask - bid, 2) if bid else None,
                 spreadPct=round((ask - bid) / ((ask + bid) / 2) * 100, 3) if bid else None,
                 takeProfitAt=round(ask + self.cfg.take_profit_pts, 2), stopLossAt=round(ask - self.cfg.stop_loss_pts, 2),
                 note="PAPER (no order); exploratory trigger crossing, not a signal", **base)

    def _update(self, t, now_ms, q):
        held = (now_ms - t.entry_ms) / 1000
        bid = q[0] if q and q[0] and q[0] > 0 else None
        if bid is None:
            t.gaps += 1
        else:
            t.ticks += 1
            t.last_bid = bid
            pnl = bid - t.entry_ask
            t.best, t.worst = max(t.best, pnl), min(t.worst, pnl)
            t.path.append([round(held, 1), bid])
            if pnl >= self.cfg.take_profit_pts:
                return self._close(t, now_ms, "TAKE_PROFIT", bid)
            if pnl <= -self.cfg.stop_loss_pts:
                return self._close(t, now_ms, "STOP_LOSS", bid)
        if held >= self.cfg.time_stop_s:
            self._close(t, now_ms, "TIME_STOP", t.last_bid)

    def _close(self, t, now_ms, outcome, exit_bid):
        self.open.remove(t)
        pnl = None if exit_bid is None else round(exit_bid - t.entry_ask, 2)
        rec = {"paperId": t.tid, "outcome": outcome, "side": t.side, "strike": t.strike, "trigger": t.trigger,
               "entryAsk": t.entry_ask, "exitBid": exit_bid, "pnlPts": pnl,
               "heldS": round((now_ms - t.entry_ms) / 1000, 1), "maxFavourablePts": round(t.best, 2),
               "maxAdversePts": round(t.worst, 2), "ticks": t.ticks, "quoteGaps": t.gaps, "path": t.path}
        if pnl is not None and self.cfg.lot_size:
            rec["pnlRsGross"] = round(pnl * self.cfg.lot_size, 2)
            if self.cfg.cost_per_trade_rs is not None:
                rec["pnlRsNet"] = round(pnl * self.cfg.lot_size - self.cfg.cost_per_trade_rs, 2)
        self.closed.append(rec)
        self.log("info", "paper_exit", note="PAPER (no order)", **rec)

    def close_all(self, now_ms: int, reason: str = "SESSION_END"):
        for t in list(self.open):
            self._close(t, now_ms, reason, t.last_bid)

    def summary(self) -> dict:
        out = {}
        for r in self.closed:
            out[r["outcome"]] = out.get(r["outcome"], 0) + 1
        decided = [r for r in self.closed if r["outcome"] in ("TAKE_PROFIT", "STOP_LOSS")]
        pnls = [r["pnlPts"] for r in self.closed if r["pnlPts"] is not None]
        return {"trades": len(self.closed), "byOutcome": out,
                "winRateTpVsSl": round(sum(r["outcome"] == "TAKE_PROFIT" for r in decided) / len(decided), 3) if decided else None,
                "breakEvenWinRate": round(self.cfg.stop_loss_pts / (self.cfg.stop_loss_pts + self.cfg.take_profit_pts), 3),
                "totalPnlPts": round(sum(pnls), 2) if pnls else 0.0,
                "note": "PAPER results; entry at ask, exit at bid; no orders were placed"}
