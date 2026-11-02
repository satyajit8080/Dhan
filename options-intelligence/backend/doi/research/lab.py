"""Strategy research laboratory (Phase 7.4).

Pipeline per strategy version (spec DSL, never free code):
  DEVELOPMENT (60%) -> VALIDATION (20%) -> WALK_FORWARD -> SENSITIVITY ->
  COST_STRESS -> sealed HOLDOUT (20%, opened exactly once) -> PAPER validation
  through the real paper engine -> human PROMOTE (rollback is always possible).

Backtests reuse the platform FeatureEngine at every bar close, so live and
historical features come from the same code. Entries fill at the NEXT bar's
open (no look-ahead). Stops are checked before targets within a bar
(conservative). Costs and slippage are explicit index-point assumptions:
this is a directional research approximation on the underlying, not an
option-premium simulation, and reports say so.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import statistics
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator

from doi.core.auth import ApprovalLog, Principal
from doi.core.canonical import canonical
from doi.core.errors import DoiError
from doi.core.market import SessionCalendar
from doi.core.models import Bar
from doi.intelligence.feature_engine.config import FeatureConfig
from doi.intelligence.feature_engine.engine import ENGINE_VERSION, FeatureEngine, FeatureInput
from doi.intelligence.feature_engine.spec import feature_specs

KNOWN_FEATURES = frozenset(s.name for s in feature_specs(FeatureConfig()))


class LabError(DoiError):
    pass


# ------------------------------------------------------------------ DSL


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    feature: str
    op: Literal[">", "<", ">=", "<=", "==", "!="]
    value: float | str | bool

    @field_validator("feature")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in KNOWN_FEATURES:
            raise ValueError(f"unknown feature {v!r}")
        return v

    def holds(self, x) -> bool:
        if x is None:
            return False
        try:
            return {">": x > self.value, "<": x < self.value, ">=": x >= self.value, "<=": x <= self.value,
                    "==": x == self.value, "!=": x != self.value}[self.op]  # fmt: skip
        except TypeError:
            return False


class StrategySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=3, max_length=80)
    direction: Literal["LONG", "SHORT"]  # on the underlying; maps to BUY_CE / BUY_PE candidates
    entry_all: tuple[Condition, ...] = Field(min_length=1, max_length=8)
    timeframe_min: int = Field(5, ge=1, le=60)
    stop_atr: float = Field(1.0, gt=0.1, le=10)
    target_atr: float = Field(1.5, gt=0.1, le=20)
    exit_after_bars: int = Field(12, ge=1, le=200)
    cost_points: float = Field(2.0, ge=0)  # round-trip cost assumption, index points
    slippage_points: float = Field(1.0, ge=0)  # per side
    max_trades_per_day: int = Field(3, ge=1, le=20)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical(self.model_dump(mode="json")).encode()).hexdigest()


# ------------------------------------------------------------------ backtester


@dataclass(frozen=True)
class SimTrade:
    entry_ts: datetime
    exit_ts: datetime
    entry: float
    exit: float
    points: float  # net of slippage and costs
    reason: str


@dataclass(frozen=True)
class Metrics:
    trades: int
    net_points: float
    win_rate: float | None
    avg_trade: float | None
    profit_factor: float | None
    max_drawdown: float
    sharpe_daily: float | None
    sortino_daily: float | None
    sessions: int

    def to_dict(self) -> dict:
        return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


def metrics(trades: Sequence[SimTrade], cal: SessionCalendar, sessions: int) -> Metrics:
    pts = [t.points for t in trades]
    wins = [p for p in pts if p > 0]
    losses = [-p for p in pts if p < 0]
    eq, peak, mdd = 0.0, 0.0, 0.0
    for p in pts:
        eq += p
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    daily: dict = {}
    for t in trades:
        daily[cal.session_date(t.exit_ts)] = daily.get(cal.session_date(t.exit_ts), 0.0) + t.points
    days = list(daily.values()) + [0.0] * max(0, sessions - len(daily))
    sharpe = sortino = None
    if len(days) >= 2 and statistics.pstdev(days) > 0:
        sharpe = statistics.mean(days) / statistics.pstdev(days) * math.sqrt(252)
        down = [min(0.0, d) for d in days]
        dd = math.sqrt(sum(x * x for x in down) / len(down))
        sortino = statistics.mean(days) / dd * math.sqrt(252) if dd > 0 else None
    return Metrics(
        len(pts), sum(pts), len(wins) / len(pts) if pts else None, sum(pts) / len(pts) if pts else None,
        (sum(wins) / sum(losses)) if losses else (None if not wins else float("inf")), mdd, sharpe, sortino, sessions,
    )  # fmt: skip


def feature_series(bars: Sequence[Bar], timeframe_min: int, cal: SessionCalendar, engine: FeatureEngine | None = None,
                   lookback_bars: int = 300) -> dict:  # fmt: skip
    """Feature vector at every bar close, each computed only from bars up to that close
    (causal), so subsets (splits, folds, variants) can share one computation."""
    engine = engine or FeatureEngine()
    bars = sorted(bars, key=lambda b: b.ts)
    tf = timedelta(minutes=timeframe_min)
    return {
        b.ts + tf: engine.calculate(
            FeatureInput("BT", timeframe_min, tuple(bars[max(0, i + 1 - lookback_bars) : i + 1]), calendar=cal), b.ts + tf
        )
        for i, b in enumerate(bars)
    }


def backtest(spec: StrategySpec, bars: Sequence[Bar], cal: SessionCalendar, engine: FeatureEngine | None = None,
             lookback_bars: int = 300, features: dict | None = None) -> list[SimTrade]:  # fmt: skip
    """Deterministic bar replay. Signals use features as of each bar close only."""
    engine = engine or FeatureEngine()
    bars = sorted(bars, key=lambda b: b.ts)
    tf = timedelta(minutes=spec.timeframe_min)
    trades: list[SimTrade] = []
    side = 1 if spec.direction == "LONG" else -1
    i, per_day = 0, {}
    while i < len(bars) - 1:
        b = bars[i]
        as_of = b.ts + tf
        day = cal.session_date(b.ts)
        if per_day.get(day, 0) >= spec.max_trades_per_day or cal.session_date(bars[i + 1].ts) != day:
            i += 1
            continue
        fv = features.get(as_of) if features is not None else None
        if fv is None:
            window = tuple(bars[max(0, i + 1 - lookback_bars) : i + 1])
            fv = engine.calculate(FeatureInput("BT", spec.timeframe_min, window, calendar=cal), as_of)
        if not all(c.holds(fv.value(c.feature)) for c in spec.entry_all):
            i += 1
            continue
        atr = fv.value("atr")
        if not atr:
            i += 1
            continue
        nxt = bars[i + 1]
        entry = nxt.open + side * spec.slippage_points
        stop = entry - side * spec.stop_atr * atr
        target = entry + side * spec.target_atr * atr
        exit_px, reason, j = None, "", i + 1
        while j < len(bars):
            bj = bars[j]
            if cal.session_date(bj.ts) != day:
                prev = bars[j - 1]
                exit_px, reason, j = prev.close, "SESSION_END", j - 1
                break
            hit_stop = bj.low <= stop if side == 1 else bj.high >= stop
            hit_tgt = bj.high >= target if side == 1 else bj.low <= target
            if hit_stop:  # conservative: stop wins when both are inside one bar
                exit_px, reason = (min(stop, bj.open) if side == 1 else max(stop, bj.open)), "STOP"
                break
            if hit_tgt:
                exit_px, reason = target, "TARGET"
                break
            if j - (i + 1) + 1 >= spec.exit_after_bars:
                exit_px, reason = bj.close, "TIME"
                break
            j += 1
        if exit_px is None:
            j = len(bars) - 1
            exit_px, reason = bars[j].close, "DATA_END"
        exit_fill = exit_px - side * spec.slippage_points
        pts = side * (exit_fill - entry) - spec.cost_points
        trades.append(SimTrade(nxt.ts, bars[j].ts, entry, exit_fill, pts, reason))
        per_day[day] = per_day.get(day, 0) + 1
        i = j + 1
    return trades


def split_sessions(
    bars: Sequence[Bar], cal: SessionCalendar, fractions=(0.6, 0.2, 0.2)
) -> tuple[list[Bar], list[Bar], list[Bar]]:
    days = sorted({cal.session_date(b.ts) for b in bars})
    if len(days) < 5:
        raise LabError(f"need at least 5 sessions, have {len(days)}")
    a = max(1, int(len(days) * fractions[0]))
    b = max(a + 1, int(len(days) * (fractions[0] + fractions[1])))
    sets = (set(days[:a]), set(days[a:b]), set(days[b:]))
    return tuple([x for x in bars if cal.session_date(x.ts) in s] for s in sets)  # type: ignore[return-value]


@dataclass(frozen=True)
class Policy:
    min_trades: int = 30
    min_profit_factor: float = 1.1
    max_validation_decay: float = 0.5  # validation avg trade >= 50% of development avg trade
    min_positive_variants: float = 0.6
    cost_stress: float = 2.0
    min_paper_trades: int = 20


def _code_version() -> str:
    v = os.environ.get("DOI_CODE_VERSION")
    if v:
        return v
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _sha(x) -> str:
    return hashlib.sha256(canonical(x).encode()).hexdigest()


# ------------------------------------------------------------------ persistence + workflow


@dataclass
class Lab:
    dsn: str
    calendar: SessionCalendar = field(default_factory=SessionCalendar)
    policy: Policy = field(default_factory=Policy)
    engine: FeatureEngine = field(default_factory=FeatureEngine)

    def _c(self):
        return psycopg.connect(self.dsn)

    def create_version(self, spec: StrategySpec, author: str, parent_version_id: str | None = None) -> str:
        with self._c() as c, c.transaction():
            c.execute(
                "INSERT INTO strategies (id, name) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING",
                ("STR-" + secrets.token_hex(5), spec.name),
            )
            sid = c.execute("SELECT id FROM strategies WHERE name = %s", (spec.name,)).fetchone()[0]
            row = c.execute(
                "SELECT id FROM strategy_versions WHERE strategy_id = %s AND spec_sha256 = %s", (sid, spec.sha256)
            ).fetchone()
            if row:
                return row[0]  # identical spec = same version (reproducibility)
            n = c.execute(
                "SELECT COALESCE(max(version), 0) + 1 FROM strategy_versions WHERE strategy_id = %s", (sid,)
            ).fetchone()[0]
            vid = f"{sid}-v{n}"
            c.execute(
                "INSERT INTO strategy_versions (id, strategy_id, version, spec, spec_sha256, parent_version_id, author) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (vid, sid, n, Jsonb(spec.model_dump(mode="json")), spec.sha256, parent_version_id, author),
            )
        return vid

    def spec(self, version_id: str) -> StrategySpec:
        with self._c() as c:
            r = c.execute("SELECT spec FROM strategy_versions WHERE id = %s", (version_id,)).fetchone()
        if r is None:
            raise LabError(f"unknown strategy version {version_id}")
        return StrategySpec.model_validate(r[0])

    def add_hypothesis(self, statement: str, author: str, version_id: str) -> str:
        hid = "HYP-" + secrets.token_hex(5)
        with self._c() as c:
            c.execute("INSERT INTO research_hypotheses (id, statement, author, strategy_version_id, status) VALUES (%s, %s, %s, %s, 'PROPOSED')",
                      (hid, statement, author, version_id))  # fmt: skip
        return hid

    def _result(self, c, eid: str, stage: str, m: dict, passed: bool) -> None:
        c.execute(
            "INSERT INTO research_results (experiment_id, stage, metrics, metrics_sha256, passed) VALUES (%s, %s, %s, %s, %s)",
            (eid, stage, Jsonb(json.loads(canonical(m))), _sha(m), passed),
        )

    def run_experiment(
        self, version_id: str, bars: Sequence[Bar], *, dataset_id: str | None = None, data_class: str = "UNSPECIFIED"
    ) -> dict:
        """Development, validation, walk-forward, sensitivity and cost stress. The holdout stays sealed."""
        spec = self.spec(version_id)
        dev, val, hold = split_sessions(bars, self.calendar)
        data_sha = _sha([(b.ts, b.open, b.high, b.low, b.close) for b in sorted(bars, key=lambda b: b.ts)])
        manifest = {
            "strategy_version_id": version_id, "spec_sha256": spec.sha256, "dataset_id": dataset_id, "data_sha256": data_sha,
            "feature_engine": ENGINE_VERSION, "feature_config_hash": self.engine.cfg.hash, "code_version": _code_version(),
            "split": {"development": [str(dev[0].ts), str(dev[-1].ts)], "validation": [str(val[0].ts), str(val[-1].ts)],
                      "holdout_sessions": len({self.calendar.session_date(b.ts) for b in hold})},
            "approximation": "directional index-point simulation; not option-premium P&L (see research_option_backtests)",
            "data_class": data_class,
        }  # fmt: skip
        fs = feature_series(bars, spec.timeframe_min, self.calendar, self.engine)  # causal; shared by every stage
        eid = "EXP-" + secrets.token_hex(5)
        nd = lambda xs: len({self.calendar.session_date(b.ts) for b in xs})
        m_dev = metrics(backtest(spec, dev, self.calendar, self.engine, features=fs), self.calendar, nd(dev)).to_dict()
        m_val = metrics(backtest(spec, val, self.calendar, self.engine, features=fs), self.calendar, nd(val)).to_dict()
        # walk-forward: fixed parameters evaluated on consecutive windows of development+validation
        dv = sorted(dev + val, key=lambda b: b.ts)
        days = sorted({self.calendar.session_date(b.ts) for b in dv})
        k = min(4, max(2, len(days) // 3))
        folds = []
        for f in range(k):
            ds = set(days[f * len(days) // k : (f + 1) * len(days) // k])
            fb = [b for b in dv if self.calendar.session_date(b.ts) in ds]
            folds.append(metrics(backtest(spec, fb, self.calendar, self.engine, features=fs), self.calendar, len(ds)).to_dict())
        wf = {"folds": folds, "positive_folds": sum(1 for x in folds if x["net_points"] > 0)}
        variants = []
        for s_mult in (0.75, 1.0, 1.25):
            for t_mult in (0.75, 1.0, 1.25):
                v = spec.model_copy(update={"stop_atr": spec.stop_atr * s_mult, "target_atr": spec.target_atr * t_mult})
                variants.append(
                    metrics(backtest(v, dv, self.calendar, self.engine, features=fs), self.calendar, len(days)).net_points
                )
        sens = {
            "variant_net_points": [round(x, 4) for x in variants],
            "positive_share": sum(1 for x in variants if x > 0) / len(variants),
        }
        stressed = spec.model_copy(
            update={
                "cost_points": spec.cost_points * self.policy.cost_stress,
                "slippage_points": spec.slippage_points * self.policy.cost_stress,
            }
        )
        m_stress = metrics(backtest(stressed, dv, self.calendar, self.engine, features=fs), self.calendar, len(days)).to_dict()

        p = self.policy
        pf = lambda m: m["profit_factor"] if m["profit_factor"] is not None else 0.0
        dev_ok = m_dev["trades"] + m_val["trades"] >= p.min_trades and pf(m_dev) >= p.min_profit_factor
        val_ok = (
            m_val["trades"] > 0
            and (m_val["avg_trade"] or 0) >= p.max_validation_decay * max(m_dev["avg_trade"] or 0, 0)
            and m_val["net_points"] > 0
        )
        wf_ok = wf["positive_folds"] >= math.ceil(len(folds) / 2)
        sens_ok = sens["positive_share"] >= p.min_positive_variants
        stress_ok = m_stress["net_points"] > 0
        passed = dev_ok and val_ok and wf_ok and sens_ok and stress_ok
        with self._c() as c, c.transaction():
            c.execute(
                "INSERT INTO research_experiments (id, strategy_version_id, dataset_id, manifest, manifest_sha256, status, data_class)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    eid,
                    version_id,
                    dataset_id,
                    Jsonb(manifest),
                    _sha(manifest),
                    "PASSED_IS" if passed else "FAILED_IS",
                    data_class,
                ),
            )
            self._result(c, eid, "DEVELOPMENT", m_dev, dev_ok)
            self._result(c, eid, "VALIDATION", m_val, val_ok)
            self._result(c, eid, "WALK_FORWARD", wf, wf_ok)
            self._result(c, eid, "SENSITIVITY", sens, sens_ok)
            self._result(c, eid, "COST_STRESS", m_stress, stress_ok)
        self._holdout_bars = {**getattr(self, "_holdout_bars", {}), eid: hold}
        return {"experiment_id": eid, "passed": passed, "manifest": manifest, "development": m_dev, "validation": m_val,
                "walk_forward": wf, "sensitivity": sens, "cost_stress": m_stress}  # fmt: skip

    def open_holdout(self, experiment_id: str, who: str, bars: Sequence[Bar] | None = None) -> dict:
        """Evaluate the sealed holdout. Allowed once, and only after in-sample stages passed."""
        with self._c() as c, c.transaction():
            r = c.execute(
                "SELECT status, strategy_version_id, manifest FROM research_experiments WHERE id = %s FOR UPDATE",
                (experiment_id,),
            ).fetchone()
            if r is None:
                raise LabError("unknown experiment")
            if c.execute("SELECT 1 FROM research_holdout_access WHERE experiment_id = %s", (experiment_id,)).fetchone():
                raise LabError("holdout already opened for this experiment")
            if r[0] != "PASSED_IS":
                raise LabError(f"holdout locked: experiment status {r[0]}")
            try:
                c.execute("INSERT INTO research_holdout_access (experiment_id, opened_by) VALUES (%s, %s)", (experiment_id, who))
            except psycopg.errors.UniqueViolation:
                raise LabError("holdout already opened for this experiment") from None
            spec = StrategySpec.model_validate(
                c.execute("SELECT spec FROM strategy_versions WHERE id = %s", (r[1],)).fetchone()[0]
            )
            hold = list(bars) if bars is not None else self._holdout_bars.get(experiment_id)
            if not hold:
                raise LabError("holdout data not available in this process; pass the same dataset bars")
            if bars is not None:
                _, _, hold = split_sessions(bars, self.calendar)
            n = len({self.calendar.session_date(b.ts) for b in hold})
            full = sorted(list(bars) if bars is not None else hold, key=lambda b: b.ts)
            fs = feature_series(full, spec.timeframe_min, self.calendar, self.engine)
            m = metrics(backtest(spec, hold, self.calendar, self.engine, features=fs), self.calendar, n).to_dict()
            ok = m["trades"] > 0 and m["net_points"] > 0 and (m["profit_factor"] or 0) > 1.0
            self._result(c, experiment_id, "HOLDOUT", m, ok)
            c.execute(
                "UPDATE research_experiments SET status = %s WHERE id = %s",
                ("PASSED_HOLDOUT" if ok else "FAILED_HOLDOUT", experiment_id),
            )
        return {"passed": ok, "holdout": m}

    def record_paper_validation(self, experiment_id: str, paper_trades: Sequence) -> dict:
        """Paper trades (doi.paper.models.Trade) whose config_version equals the strategy version id."""
        with self._c() as c, c.transaction():
            r = c.execute(
                "SELECT status, strategy_version_id FROM research_experiments WHERE id = %s FOR UPDATE", (experiment_id,)
            ).fetchone()
            if r is None or r[0] not in ("PASSED_HOLDOUT", "PAPER_VALIDATION"):
                raise LabError("paper validation requires a passed holdout")
            mine = [t for t in paper_trades if t.config_version == r[1]]
            net = float(sum(t.net_pnl for t in mine))
            m = {"paper_trades": len(mine), "net_pnl": round(net, 2), "wins": sum(1 for t in mine if t.net_pnl > 0)}
            ok = len(mine) >= self.policy.min_paper_trades and net >= 0
            self._result(c, experiment_id, "PAPER", m, ok)
            c.execute(
                "UPDATE research_experiments SET status = %s WHERE id = %s",
                ("PAPER_PASSED" if ok else "PAPER_FAILED", experiment_id),
            )
        return {"passed": ok, **m}

    def promote(self, experiment_id: str, who: Principal, approvals: ApprovalLog, note: str | None = None) -> None:
        with self._c() as c, c.transaction():
            r = c.execute(
                "SELECT e.status, e.strategy_version_id, v.strategy_id FROM research_experiments e JOIN strategy_versions v ON v.id = e.strategy_version_id"
                " WHERE e.id = %s",
                (experiment_id,),
            ).fetchone()
            if r is None or r[0] != "PAPER_PASSED":
                raise LabError("promotion requires passed holdout and paper validation")
            rec = approvals.append_tx(c, who, "PROMOTE", "strategy_version", r[1], note, {"experiment_id": experiment_id})
            c.execute("INSERT INTO strategy_activations (strategy_id, version_id, action, actor, approval_seq) VALUES (%s, %s, 'PROMOTE', %s, %s)",
                      (r[2], r[1], who.actor, rec.seq))  # fmt: skip
            c.execute("UPDATE strategies SET active_version_id = %s WHERE id = %s", (r[1], r[2]))

    def rollback(
        self, strategy_id: str, to_version_id: str, who: Principal, approvals: ApprovalLog, note: str | None = None
    ) -> None:
        with self._c() as c, c.transaction():
            ok = c.execute(
                "SELECT 1 FROM strategy_activations WHERE strategy_id = %s AND version_id = %s AND action = 'PROMOTE'",
                (strategy_id, to_version_id),
            ).fetchone()
            if not ok:
                raise LabError("can only roll back to a version that was previously promoted")
            rec = approvals.append_tx(c, who, "ROLLBACK", "strategy", strategy_id, note, {"to_version_id": to_version_id})
            c.execute("INSERT INTO strategy_activations (strategy_id, version_id, action, actor, approval_seq) VALUES (%s, %s, 'ROLLBACK', %s, %s)",
                      (strategy_id, to_version_id, who.actor, rec.seq))  # fmt: skip
            c.execute("UPDATE strategies SET active_version_id = %s WHERE id = %s", (to_version_id, strategy_id))

    def history(self, strategy_id: str) -> dict:
        with self._c() as c:
            versions = c.execute(
                "SELECT v.id, v.version, v.spec_sha256, v.author, v.created_at FROM strategy_versions v WHERE v.strategy_id = %s ORDER BY v.version",
                (strategy_id,),
            ).fetchall()
            results = c.execute(
                "SELECT e.strategy_version_id, e.id, r.stage, r.metrics, r.passed FROM research_experiments e"
                " JOIN research_results r ON r.experiment_id = e.id JOIN strategy_versions v ON v.id = e.strategy_version_id"
                " WHERE v.strategy_id = %s ORDER BY e.created_at, r.stage",
                (strategy_id,),
            ).fetchall()
            acts = c.execute("SELECT seq, version_id, action, actor, created_at FROM strategy_activations WHERE strategy_id = %s ORDER BY seq",
                             (strategy_id,)).fetchall()  # fmt: skip
            active = c.execute("SELECT active_version_id FROM strategies WHERE id = %s", (strategy_id,)).fetchone()[0]
        comparison: dict = {}
        for vid, eid, stage, m, passed in results:
            comparison.setdefault(vid, {}).setdefault(eid, {})[stage] = {"passed": passed, "net_points": m.get("net_points"),
                                                                          "trades": m.get("trades"), "profit_factor": m.get("profit_factor")}  # fmt: skip
        return {"active_version_id": active, "versions": [dict(zip(("id", "version", "spec_sha256", "author", "created_at"), v, strict=True)) for v in versions],
                "comparison": comparison, "activations": [dict(zip(("seq", "version_id", "action", "actor", "at"), a, strict=True)) for a in acts]}  # fmt: skip


def generate_hypothesis(gateway, context: dict, now=None):
    """Ask the Quant Research Lead for a testable StrategySpec.

    Hypotheses are PROPOSALS TO BE TESTED, not financial claims. They are validated
    for schema and known feature names only. Thresholds need not appear in the
    evidence, because the lab measures them out of sample before anyone acts."""
    import asyncio as _a  # noqa: F401

    from doi.ai.agents import REGISTRY
    from doi.llm.gateway import LLMRequest, extract_json

    spec = REGISTRY["quant_lead"]
    system = (
        "You design ONE testable intraday strategy for Indian index research, expressed ONLY in the given JSON schema. "
        "Use only these features: "
        + ", ".join(sorted(KNOWN_FEATURES))
        + ". No live trading exists; the lab will test your idea.\n"
        + json.dumps(StrategySpec.model_json_schema(), separators=(",", ":"))
    )
    user = "<evidence>\n" + canonical(context) + "\n</evidence>"

    async def go():
        res = await gateway.complete(LLMRequest(purpose="lab:hypothesis", system=system, user=user, preferred_tier=spec.tier,
                                                max_tokens=700, agent_id=spec.id, agent_version=spec.version), now)  # fmt: skip
        raw = extract_json(res.text)
        try:
            return StrategySpec.model_validate(raw) if raw else None
        except Exception:  # invalid proposals are discarded, never repaired by guesswork
            return None

    return go()
