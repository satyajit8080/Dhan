"""Independent agent scoring and leaderboard (Phase 7.5).

Only Python computes these numbers, from pre-registered predictions and market
observations recorded at or after each prediction's resolve time (enforced by a
database trigger). No agent can influence its own score. LIVE and REPLAY results
are always kept separate.

Per agent (and per version):
  - hit rate, and its Wilson 95% lower bound (used for ranking, which penalises small samples);
  - Brier score, mapping conviction 1-5 to 0.55/0.60/0.65/0.75/0.85 (mapping is a fixed convention, not calibration);
  - calibration buckets: realised hit rate per conviction level;
  - information coefficient: Spearman correlation of signed conviction vs realised return;
  - follow-the-agent risk-adjusted return: mean/stdev of sign(direction) x return (FLAT = 0), and a downside version;
  - for the Contrarian: veto save rate, the share of vetoed meetings whose CIO call then missed.
Agents with fewer than `min_n` resolved predictions are listed but marked provisional.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg
from psycopg.types.json import Jsonb

CONF_PROB = {1: 0.55, 2: 0.60, 3: 0.65, 4: 0.75, 5: 0.85}


@dataclass(frozen=True)
class Outcome:
    prediction_id: str
    agent_id: str
    agent_version: str
    meeting_id: str | None
    direction: str
    confidence: int
    signed_return: float
    hit: bool
    brier: float


def realized_direction(ret: float, flat_band: float) -> str:
    return "UP" if ret > flat_band else "DOWN" if ret < -flat_band else "FLAT"


def score_one(
    direction: str, confidence: int, ref: float, realized: float, flat_band: float = 0.0005
) -> tuple[float, bool, float]:
    ret = (realized - ref) / ref
    hit = realized_direction(ret, flat_band) == direction
    p = CONF_PROB[confidence]
    return ret, hit, (p - (1.0 if hit else 0.0)) ** 2


def wilson_lower(hits: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    p = hits / n
    return (p + z * z / (2 * n) - z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / (1 + z * z / n)


def _rank(xs: Sequence[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(a: Sequence[float], b: Sequence[float]) -> float | None:
    if len(a) < 3:
        return None
    ra, rb = _rank(a), _rank(b)
    sa, sb = statistics.pstdev(ra), statistics.pstdev(rb)
    if sa == 0 or sb == 0:
        return None
    ma, mb = statistics.mean(ra), statistics.mean(rb)
    return sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=True)) / (len(a) * sa * sb)


def agent_metrics(outs: Sequence[Outcome]) -> dict:
    n = len(outs)
    hits = sum(o.hit for o in outs)
    sign = {"UP": 1, "DOWN": -1, "FLAT": 0}
    follow = [sign[o.direction] * o.signed_return for o in outs]
    calib = {}
    for c in range(1, 6):
        b = [o for o in outs if o.confidence == c]
        if b:
            calib[str(c)] = {"n": len(b), "hit_rate": round(sum(o.hit for o in b) / len(b), 4), "assumed_p": CONF_PROB[c]}
    downside = [min(0.0, x) for x in follow]
    dd = math.sqrt(sum(x * x for x in downside) / n) if n else 0.0
    sd = statistics.pstdev(follow) if n > 1 else 0.0
    return {
        "n": n,
        "hit_rate": round(hits / n, 4) if n else None,
        "hit_rate_wilson_lb": round(wilson_lower(hits, n), 4),
        "brier": round(statistics.mean(o.brier for o in outs), 6) if n else None,
        "calibration": calib,
        "ic_spearman": (lambda v: None if v is None else round(v, 4))(
            spearman([sign[o.direction] * o.confidence for o in outs], [o.signed_return for o in outs])
        ),
        "follow_mean_return": round(statistics.mean(follow), 8) if n else None,
        "follow_ratio": round(statistics.mean(follow) / sd, 4) if n > 1 and sd > 0 else None,
        "follow_downside_ratio": round(statistics.mean(follow) / dd, 4) if n and dd > 0 else None,
    }


def leaderboard(
    outs: Sequence[Outcome], *, min_n: int = 30, by_version: bool = False, contrarian_saves: dict | None = None
) -> list[dict]:
    groups: dict[tuple[str, str], list[Outcome]] = defaultdict(list)
    for o in outs:
        groups[(o.agent_id, o.agent_version if by_version else "*")].append(o)
    rows = []
    for (aid, ver), g in groups.items():
        m = agent_metrics(g)
        if aid == "contrarian" and contrarian_saves is not None:
            m["veto_save_rate"] = contrarian_saves.get("rate")
            m["vetoes_resolved"] = contrarian_saves.get("n")
        rows.append({"agent_id": aid, "agent_version": ver, "provisional": m["n"] < min_n, **m})
    ranked = sorted(
        (r for r in rows if not r["provisional"]), key=lambda r: (-r["hit_rate_wilson_lb"], r["brier"] or 1.0, r["agent_id"])
    )
    for i, r in enumerate(ranked, start=1):
        r["rank"] = i
    prov = sorted((r for r in rows if r["provisional"]), key=lambda r: (-r["n"], r["agent_id"]))
    for r in prov:
        r["rank"] = None
    return ranked + prov


def contrarian_save_rate(vetoed_meetings: set[str], cio_outcomes: dict[str, bool]) -> dict:
    """Of meetings where the contrarian vetoed and the CIO's call was resolved: the share where the CIO missed."""
    resolved = [m for m in vetoed_meetings if m in cio_outcomes]
    return {
        "n": len(resolved),
        "rate": round(sum(1 for m in resolved if not cio_outcomes[m]) / len(resolved), 4) if resolved else None,
    }


# ------------------------------------------------------------------ PostgreSQL


class PgScoring:
    def __init__(self, dsn: str, flat_band: float = 0.0005) -> None:
        self.dsn, self.flat_band = dsn, flat_band

    def _observation(
        self, c, underlying: str, index_sid: str | None, resolve_at: datetime, max_wait: timedelta
    ) -> tuple[float, datetime] | None:
        """First recorded observation at or after resolve_at: index LTP from a chain snapshot, or an index bar close."""
        r = c.execute(
            "SELECT underlying_ltp, COALESCE(source_ts, received_at) AS t FROM option_chain_snapshots WHERE underlying_key = %s"
            " AND COALESCE(source_ts, received_at) >= %s AND COALESCE(source_ts, received_at) <= %s AND underlying_ltp IS NOT NULL"
            " ORDER BY t LIMIT 1",
            (underlying, resolve_at, resolve_at + max_wait),
        ).fetchone()
        if r:
            return float(r[0]), r[1]
        if index_sid:
            b = c.execute(
                "SELECT close, bar_ts + (regexp_replace(timeframe, 'm$', '') || ' minutes')::interval AS closed FROM market_bars"
                " WHERE security_id = %s AND bar_ts + (regexp_replace(timeframe, 'm$', '') || ' minutes')::interval >= %s"
                " AND bar_ts <= %s ORDER BY closed LIMIT 1",
                (index_sid, resolve_at, resolve_at + max_wait),
            ).fetchone()
            if b:
                return float(b[0]), b[1]
        return None

    def resolve_due(
        self, now: datetime, index_ids: dict[str, str] | None = None, max_wait: timedelta = timedelta(hours=2)
    ) -> int:
        index_ids = index_ids or {}
        n = 0
        with psycopg.connect(self.dsn) as c:
            due = c.execute(
                "SELECT p.id, p.underlying_key, p.direction, p.confidence, p.ref_value, p.resolve_at FROM ai_predictions p"
                " LEFT JOIN ai_prediction_outcomes o ON o.prediction_id = p.id WHERE o.prediction_id IS NULL AND p.resolve_at <= %s ORDER BY p.resolve_at",
                (now,),
            ).fetchall()
            for pid, u, direction, conf, ref, resolve_at in due:
                obs = self._observation(c, u, index_ids.get(u), resolve_at, max_wait)
                if obs is None:
                    continue  # no observation yet: stays unresolved (never guessed)
                val, at = obs
                ret, hit, brier = score_one(direction, conf, float(ref), val, self.flat_band)
                with c.transaction():
                    c.execute(
                        "INSERT INTO ai_prediction_outcomes (prediction_id, resolved_at, realized_value, observed_at, signed_return, hit, brier)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                        (pid, max(now, at), val, at, ret, hit, brier),
                    )
                n += 1
        return n

    def outcomes(self, mode: str = "LIVE") -> list[Outcome]:
        with psycopg.connect(self.dsn) as c:
            rows = c.execute(
                "SELECT p.id, p.agent_id, p.agent_version, p.meeting_id, p.direction, p.confidence, o.signed_return, o.hit, o.brier"
                " FROM ai_predictions p JOIN ai_prediction_outcomes o ON o.prediction_id = p.id WHERE p.mode = %s",
                (mode,),
            ).fetchall()
        return [Outcome(r[0], r[1], r[2], r[3], r[4], r[5], float(r[6]), r[7], float(r[8])) for r in rows]

    def contrarian_saves(self, mode: str = "LIVE") -> dict:
        with psycopg.connect(self.dsn) as c:
            vetoed = {r[0] for r in c.execute(
                "SELECT t.meeting_id FROM ai_meeting_turns t WHERE t.agent_id = 'contrarian' AND t.status = 'OK' AND (t.output->>'veto')::boolean")}  # fmt: skip
        cio = {o.meeting_id: o.hit for o in self.outcomes(mode) if o.agent_id == "cio" and o.meeting_id}
        return contrarian_save_rate(vetoed, cio)

    def leaderboard(self, mode: str = "LIVE", *, min_n: int = 30, by_version: bool = False) -> list[dict]:
        return leaderboard(self.outcomes(mode), min_n=min_n, by_version=by_version, contrarian_saves=self.contrarian_saves(mode))

    def snapshot(self, now: datetime, mode: str = "LIVE", min_n: int = 30) -> int:
        rows = self.leaderboard(mode, min_n=min_n) + self.leaderboard(mode, min_n=min_n, by_version=True)
        with psycopg.connect(self.dsn) as c, c.transaction():
            for r in rows:
                c.execute(
                    "INSERT INTO ai_agent_scorecards (computed_at, mode, agent_id, agent_version, n, metrics, rank, provisional)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (now, mode, r["agent_id"], r["agent_version"], r["n"], Jsonb({k: v for k, v in r.items() if k not in ("agent_id", "agent_version")}),
                     r["rank"], r["provisional"]),
                )  # fmt: skip
        return len(rows)
