"""FeatureEngine.calculate(input, as_of, config) -> immutable FeatureVector.

Look-ahead safety: the engine first discards everything not known at `as_of`:
  - a bar is known once it has closed (`bar.ts + timeframe <= as_of`);
  - a chain snapshot is known once received (`source_ts or received_at <= as_of`).
The result is identical whether or not future data was passed in, and a test
enforces this.

Traceability: every FeatureValue carries the calculation id, its parameters and
the ids of the exact inputs it used. The vector carries a SHA-256 digest over
everything, so later evidence packs can cite features by digest.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from doi.analytics import black76
from doi.analytics import options as opt
from doi.analytics import technical as ta
from doi.core.canonical import canonical
from doi.core.market import SessionCalendar
from doi.core.models import Bar, ChainRow, OptionChainSnapshot
from doi.intelligence.feature_engine.config import FeatureConfig
from doi.intelligence.feature_engine.spec import feature_specs

ENGINE_VERSION = "fe-1"
Scalar = float | int | str | bool | None


class Status(StrEnum):
    OK = "OK"
    WARMUP = "WARMUP"
    NULL = "NULL"  # computable in principle, but undefined for these inputs
    NO_DATA = "NO_DATA"
    STALE = "STALE"  # value computed from a snapshot older than max_snapshot_age_s


@dataclass(frozen=True, slots=True)
class Provenance:
    calc: str
    params: tuple[tuple[str, Any], ...]
    sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FeatureValue:
    value: Scalar
    status: Status
    provenance: Provenance
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class FeatureInput:
    underlying_key: str
    timeframe_min: int
    bars: tuple[Bar, ...]
    snapshots: tuple[OptionChainSnapshot, ...] = ()
    vix_bars: tuple[Bar, ...] = ()
    vix_timeframe_min: int | None = None
    calendar: SessionCalendar = field(default_factory=SessionCalendar)
    expiry: date | None = None  # defaults to the latest snapshot's expiry
    lot_size: int | None = None  # needed only when cfg.oi_unit == "contracts"


@dataclass(frozen=True)
class FeatureVector:
    underlying_key: str
    timeframe_min: int
    as_of: datetime
    engine_version: str
    config_version: str
    config_hash: str
    inputs_digest: str
    data_quality_score: float | None
    coverage_status: str
    values: Mapping[str, FeatureValue]

    def value(self, name: str) -> Scalar:
        return self.values[name].value

    def to_dict(self) -> dict:
        return {
            "underlying_key": self.underlying_key,
            "timeframe_min": self.timeframe_min,
            "as_of": self.as_of,
            "engine_version": self.engine_version,
            "config_version": self.config_version,
            "config_hash": self.config_hash,
            "inputs_digest": self.inputs_digest,
            "data_quality_score": self.data_quality_score,
            "coverage_status": self.coverage_status,
            "values": {
                k: {"value": v.value, "status": v.status, "reason": v.reason, "provenance": v.provenance}
                for k, v in self.values.items()
            },
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical(self.to_dict()).encode()).hexdigest()

    @property
    def version_key(self) -> str:
        return f"{self.engine_version}:{self.config_version}:{self.config_hash[:12]}"


def _clean(x: float | None) -> float | None:
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else x


def _snap_ts(s: OptionChainSnapshot) -> datetime:
    return s.source_ts or s.received_at


def _bars_id(kind: str, bars: Sequence[Bar], tf: int | None) -> str:
    if not bars:
        return f"{kind}:none"
    return f"{kind}:{tf}m:{bars[0].ts.isoformat()}..{bars[-1].ts.isoformat()}#{len(bars)}"


def _snap_id(s: OptionChainSnapshot) -> str:
    rows = hashlib.sha256(canonical(s.rows).encode()).hexdigest()[:16]
    return f"chain:{s.underlying_key}:{s.expiry.isoformat()}:{_snap_ts(s).isoformat()}:{rows}"


@dataclass
class _ChainCtx:
    snap: OptionChainSnapshot
    sid: str
    t: float | None
    df: float
    forward: float | None
    fwd_n: int
    center: float | None
    atm: float | None
    step: float | None
    win: list[ChainRow]


class FeatureEngine:
    def __init__(self, config: FeatureConfig | None = None) -> None:
        self.cfg = config or FeatureConfig()
        self.specs = feature_specs(self.cfg)

    # ---------------------------------------------------------------- public

    def calculate(self, inp: FeatureInput, as_of: datetime) -> FeatureVector:
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        cfg, tf = self.cfg, inp.timeframe_min
        if tf < 1:
            raise ValueError("timeframe must be >= 1 minute")
        step = timedelta(minutes=tf)
        bars = self._dedupe([b for b in inp.bars if b.ts + step <= as_of])
        vtf = timedelta(minutes=inp.vix_timeframe_min or tf)
        vix = self._dedupe([b for b in inp.vix_bars if b.ts + vtf <= as_of])
        snaps = sorted((s for s in inp.snapshots if _snap_ts(s) <= as_of), key=_snap_ts)

        out: dict[str, FeatureValue] = {}
        self._technical(out, bars, inp, as_of, snaps)
        self._vix(out, vix, inp)
        chain_cov = self._options(out, snaps, inp, as_of)
        self._time(out, snaps, inp, as_of)
        self._regime(out, bars, inp)

        names = [s.name for s in self.specs]
        if set(out) != set(names):  # internal guard: every declared feature, nothing undeclared
            raise AssertionError(f"feature set mismatch: missing {set(names) - set(out)}, extra {set(out) - set(names)}")
        ordered = {n: out[n] for n in names}
        digest_src = {
            "bars": [(b.ts, b.open, b.high, b.low, b.close, b.volume, b.oi) for b in bars],
            "vix": [(b.ts, b.close) for b in vix],
            "snapshots": [_snap_id(s) for s in snaps],
            "calendar": sorted(inp.calendar.holidays),
            "expiry": inp.expiry,
            "lot_size": inp.lot_size,
        }
        score, coverage = chain_cov
        return FeatureVector(
            underlying_key=inp.underlying_key,
            timeframe_min=tf,
            as_of=as_of,
            engine_version=ENGINE_VERSION,
            config_version=cfg.version,
            config_hash=cfg.hash,
            inputs_digest=hashlib.sha256(canonical(digest_src).encode()).hexdigest(),
            data_quality_score=score,
            coverage_status=coverage,
            values=MappingProxyType(ordered),
        )

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _dedupe(bars: list[Bar]) -> list[Bar]:
        seen: dict[datetime, Bar] = {}
        for b in sorted(bars, key=lambda b: b.ts):
            seen.setdefault(b.ts, b)  # first occurrence wins, deterministically
        return list(seen.values())

    @staticmethod
    def _v(
        value: Scalar,
        calc: str,
        sources: Sequence[str],
        params: dict | None = None,
        *,
        warm: bool = False,
        null_reason: str | None = None,
        stale: bool = False,
    ) -> FeatureValue:
        prov = Provenance(calc, tuple(sorted((params or {}).items())), tuple(sources))
        if isinstance(value, float):
            value = _clean(value)
        if value is None:
            if warm:
                return FeatureValue(None, Status.WARMUP, prov, null_reason or "insufficient history")
            return FeatureValue(
                None, Status.NO_DATA if null_reason is None else Status.NULL, prov, null_reason or "no input data"
            )
        return FeatureValue(value, Status.STALE if stale else Status.OK, prov, "snapshot older than max age" if stale else None)

    # ------------------------------------------------------------- technical

    def _technical(
        self, out: dict, bars: list[Bar], inp: FeatureInput, as_of: datetime, snaps: list[OptionChainSnapshot]
    ) -> None:
        cfg, tf, v = self.cfg, inp.timeframe_min, self._v
        src = [_bars_id("bars", bars, tf)]
        closes = [b.close for b in bars]
        last = lambda series: series[-1] if series else None
        have = bool(bars)

        for n in cfg.ema_periods:
            out[f"ema_{n}"] = v(last(ta.ema(closes, n)), "ema@1", src, {"n": n}, warm=have)
        session = [b for b in bars if inp.calendar.session_date(b.ts) == inp.calendar.session_date(as_of)]
        ssrc = [_bars_id("session_bars", session, tf)]
        has_vol = ta.has_volume(session)
        no_vol = None if has_vol or not session else "bars carry no volume (index candles); supply futures bars"
        vw = ta.vwap(session) if has_vol else None
        out["vwap"] = v(vw, "vwap@1", ssrc, null_reason=no_vol)
        out["vwap_distance_pct"] = v(
            None if vw is None or not closes else (closes[-1] - vw) / vw * 100, "pct_distance@1", ssrc + src, null_reason=no_vol
        )
        a, p, m = ta.adx(bars, cfg.adx_period)
        out["adx"] = v(last(a), "adx_wilder@1", src, {"n": cfg.adx_period}, warm=have)
        out["plus_di"] = v(last(p), "di_wilder@1", src, {"n": cfg.adx_period}, warm=have)
        out["minus_di"] = v(last(m), "di_wilder@1", src, {"n": cfg.adx_period}, warm=have)
        out["market_structure"] = v(
            ta.market_structure(bars, cfg.swing_k), "fractal_structure@1", src, {"k": cfg.swing_k}, warm=have
        )
        out["rsi"] = v(last(ta.rsi(closes, cfg.rsi_period)), "rsi_wilder@1", src, {"n": cfg.rsi_period}, warm=have)
        line, sig, hist = ta.macd(closes, *cfg.macd)
        mp = dict(zip(("fast", "slow", "signal"), cfg.macd, strict=True))
        out["macd"] = v(last(line), "macd@1", src, mp, warm=have)
        out["macd_signal"] = v(last(sig), "macd@1", src, mp, warm=have)
        out["macd_hist"] = v(last(hist), "macd@1", src, mp, warm=have)
        out["roc"] = v(last(ta.roc(closes, cfg.roc_period)), "roc@1", src, {"n": cfg.roc_period}, warm=have)
        at = ta.atr(bars, cfg.atr_period)
        atr_last = last(at)
        out["atr"] = v(atr_last, "atr_wilder@1", src, {"n": cfg.atr_period}, warm=have)
        out["atr_pct"] = v(
            None if atr_last is None else atr_last / closes[-1] * 100, "atr_pct@1", src, {"n": cfg.atr_period}, warm=have
        )
        out["bb_width_pct"] = v(
            last(ta.bollinger_width(closes, cfg.bb_period, cfg.bb_k)),
            "bb_width@1",
            src,
            {"n": cfg.bb_period, "k": cfg.bb_k},
            warm=have,
        )
        ppy = cfg.trading_days_per_year * cfg.session_minutes / tf
        out["realized_vol_pct"] = v(
            last(ta.realized_vol(closes, cfg.rv_window, ppy)),
            "realized_vol@1",
            src,
            {"n": cfg.rv_window, "periods_per_year": ppy},
            warm=have,
        )

        rv = ta.relative_volume(bars, cfg.rel_volume_window) if ta.has_volume(bars) else None
        rv_null = None if ta.has_volume(bars) or not bars else "bars carry no volume (index candles); supply futures bars"
        out["relative_volume"] = v(
            rv, "relative_volume@1", src, {"n": cfg.rel_volume_window}, warm=rv_null is None and have, null_reason=rv_null
        )
        snap_step = None
        if snaps:  # only snapshots known at as_of (never inp.snapshots: look-ahead)
            snap_step = opt.strike_step(snaps[-1].rows)
        bin_size = (snap_step or 50.0) * cfg.volume_bin_steps
        out["volume_poc"] = v(
            ta.volume_poc(session, bin_size) if has_vol else None, "volume_poc@1", ssrc, {"bin": bin_size}, null_reason=no_vol
        )

        open_ts = inp.calendar.at(inp.calendar.session_date(as_of), inp.calendar.open)
        or_end = open_ts + timedelta(minutes=cfg.opening_range_min)
        or_bars = [b for b in session if b.ts < or_end]
        complete = bool(or_bars) and as_of >= or_end and any(b.ts + timedelta(minutes=tf) >= or_end for b in or_bars)
        orp = {"minutes": cfg.opening_range_min}
        orsrc = [_bars_id("opening_bars", or_bars, tf)]
        hi = max(b.high for b in or_bars) if complete else None
        lo = min(b.low for b in or_bars) if complete else None
        out["opening_range_high"] = v(hi, "opening_range@1", orsrc, orp, warm=bool(session))
        out["opening_range_low"] = v(lo, "opening_range@1", orsrc, orp, warm=bool(session))
        state = None
        if hi is not None and session:
            c = session[-1].close
            state = "ABOVE" if c > hi else "BELOW" if c < lo else "INSIDE"
        out["opening_range_state"] = v(state, "opening_range@1", orsrc + ssrc, orp, warm=bool(session))

        cc = (
            ta.candle_context(bars[-1], atr_last)
            if bars
            else {"range_expansion": None, "body_ratio": None, "close_location": None}
        )
        lsrc = [_bars_id("last_bar", bars[-1:], tf)]
        out["range_expansion"] = v(cc["range_expansion"], "range_expansion@1", lsrc + src, warm=have)
        out["body_ratio"] = v(cc["body_ratio"], "body_ratio@1", lsrc, null_reason="zero-range bar" if bars else None)
        out["close_location"] = v(cc["close_location"], "close_location@1", lsrc, null_reason="zero-range bar" if bars else None)

    def _vix(self, out: dict, vix: list[Bar], inp: FeatureInput) -> None:
        src = [_bars_id("vix", vix, inp.vix_timeframe_min or inp.timeframe_min)]
        out["vix"] = self._v(vix[-1].close if vix else None, "last_close@1", src)
        chg = None
        if vix:
            today = inp.calendar.session_date(vix[-1].ts)
            prior = [b for b in vix if inp.calendar.session_date(b.ts) < today]
            if prior and prior[-1].close:
                chg = (vix[-1].close / prior[-1].close - 1) * 100
        out["vix_change_pct"] = self._v(chg, "pct_change_prev_session@1", src, warm=bool(vix))

    # --------------------------------------------------------------- options

    def _ctx(self, s: OptionChainSnapshot, as_of: datetime, inp: FeatureInput) -> _ChainCtx:
        cfg = self.cfg
        expiry = inp.expiry or s.expiry
        exp_ts = inp.calendar.at(expiry, time(15, 30))
        ref_ts = _snap_ts(s)
        t = (exp_ts - ref_ts).total_seconds() / (365 * 86400)
        t = t if t > 0 else None
        df = opt.discount_factor(cfg.rate, t or 0.0)
        ref = s.underlying_ltp
        fwd, n = opt.implied_forward(s.rows, ref, df, cfg.parity_band_pct) if ref else (None, 0)
        center = fwd or ref
        atm = opt.atm_strike(s.rows, center) if center else None
        win = opt.window(s.rows, center, cfg.pcr_strike_window) if center else []
        return _ChainCtx(s, _snap_id(s), t, df, fwd, n, center, atm, opt.strike_step(s.rows), win)

    @staticmethod
    def _snap_before(snaps: list[OptionChainSnapshot], target: datetime, tolerance: timedelta) -> OptionChainSnapshot | None:
        cands = [s for s in snaps if target - tolerance <= _snap_ts(s) <= target]
        return cands[-1] if cands else None

    def _options(
        self, out: dict, snaps: list[OptionChainSnapshot], inp: FeatureInput, as_of: datetime
    ) -> tuple[float | None, str]:
        cfg, v = self.cfg, self._v
        names = [s.name for s in self.specs if s.family in ("options", "liquidity")]
        if not snaps:
            for n in names:
                out[n] = v(None, "chain@1", ["chain:none"])
            return None, "NO_CHAIN"
        cur = self._ctx(snaps[-1], as_of, inp)
        age = (as_of - _snap_ts(cur.snap)).total_seconds()
        stale = age > cfg.max_snapshot_age_s
        src = [cur.sid]
        sv = lambda name, value, calc, sources=src, params=None, null=None: out.__setitem__(
            name, v(value, calc, sources, params, null_reason=null, stale=stale)
        )

        sv(
            "forward",
            cur.forward,
            "parity_forward@1",
            params={"band_pct": cfg.parity_band_pct, "rate": cfg.rate},
            null="no two-sided pairs within band",
        )
        sv(
            "forward_strikes_used",
            cur.fwd_n if cur.forward is not None else None,
            "parity_forward@1",
            null="no two-sided pairs within band",
        )
        sv(
            "forward_basis",
            None if cur.forward is None or not cur.snap.underlying_ltp else cur.forward - cur.snap.underlying_ltp,
            "basis@1",
            null="forward or index LTP missing",
        )
        sv("atm_strike", cur.atm, "atm_nearest_forward@1")
        sv("strike_step", cur.step, "strike_step_mode@1")
        sv("time_to_expiry_years", cur.t, "act365_to_1530_ist@1", null="at or past expiry")
        wp = {"strikes_each_side": cfg.pcr_strike_window}
        pcr_oi, pcr_vol = opt.pcr(cur.win, "oi"), opt.pcr(cur.win, "volume")
        sv("pcr_oi", pcr_oi, "pcr_oi@1", params=wp, null="zero call OI in window")
        sv("pcr_oi_all", opt.pcr(cur.snap.rows, "oi"), "pcr_oi@1", null="zero call OI")
        sv("pcr_volume", pcr_vol, "pcr_volume@1", params=wp, null="zero call volume in window")

        def past_pcr(s: OptionChainSnapshot | None) -> tuple[float | None, float | None, str | None]:
            if s is None:
                return None, None, None
            c = self._ctx(s, as_of, inp)
            return opt.pcr(c.win, "oi"), opt.pcr(c.win, "volume"), c.sid

        for w in cfg.pcr_change_windows_min:
            target = as_of - timedelta(minutes=w)
            ps = self._snap_before(snaps[:-1], target, timedelta(minutes=max(1.0, w / 2)))
            poi, pvol, psid = past_pcr(ps)
            srcs = src + ([psid] if psid else [])
            out[f"pcr_oi_chg_{w}m"] = v(
                None if poi is None or pcr_oi is None else pcr_oi - poi,
                "pcr_change@1",
                srcs,
                {**wp, "window_min": w},
                warm=True,
                stale=stale,
            )
            out[f"pcr_vol_chg_{w}m"] = v(
                None if pvol is None or pcr_vol is None else pcr_vol - pvol,
                "pcr_change@1",
                srcs,
                {**wp, "window_min": w},
                warm=True,
                stale=stale,
            )
        today = inp.calendar.session_date(as_of)
        session_snaps = [s for s in snaps if inp.calendar.session_date(_snap_ts(s)) == today]
        first = session_snaps[0] if len(session_snaps) > 1 else None
        foi, fvol, fsid = past_pcr(first)
        fsrc = src + ([fsid] if fsid else [])
        out["pcr_oi_chg_session"] = v(
            None if foi is None or pcr_oi is None else pcr_oi - foi, "pcr_change@1", fsrc, wp, warm=True, stale=stale
        )
        out["pcr_vol_chg_session"] = v(
            None if fvol is None or pcr_vol is None else pcr_vol - fvol, "pcr_change@1", fsrc, wp, warm=True, stale=stale
        )

        prev_wall_snap = self._snap_before(
            snaps[:-1], as_of - timedelta(minutes=cfg.wall_lookback_min), timedelta(minutes=cfg.wall_lookback_min)
        )
        for side, leg in (("put", "pe"), ("call", "ce")):
            w_now = opt.wall(cur.snap.rows, leg)
            dist = None if w_now is None or not cur.center else (w_now.strike - cur.center) / cur.center * 100
            sv(f"{side}_wall", w_now.strike if w_now else None, "oi_wall@1", null="no OI on side")
            sv(f"{side}_wall_share", w_now.share if w_now else None, "oi_wall@1", null="no OI on side")
            sv(f"{side}_wall_distance_pct", dist, "oi_wall_distance@1", null="no OI on side")
            mig = None
            msrc = list(src)
            if prev_wall_snap is not None and w_now is not None and cur.step:
                w_prev = opt.wall(prev_wall_snap.rows, leg)
                msrc.append(_snap_id(prev_wall_snap))
                if w_prev is not None:
                    mig = round((w_now.strike - w_prev.strike) / cur.step)
            out[f"{side}_wall_migration_steps"] = v(
                mig, "wall_migration@1", msrc, {"lookback_min": cfg.wall_lookback_min}, warm=True, stale=stale
            )

        mp = opt.max_pain(cur.snap.rows)
        sv("max_pain", mp, "max_pain@1", null="no OI")
        sv(
            "max_pain_distance_pct",
            None if mp is None or not cur.center else (mp - cur.center) / cur.center * 100,
            "pct_distance@1",
            null="no OI or forward",
        )
        mp_first = opt.max_pain(first.rows) if first is not None else None
        out["max_pain_change"] = v(
            None if mp is None or mp_first is None else mp - mp_first, "max_pain_change@1", fsrc, warm=True, stale=stale
        )

        self._vol(out, cur, sv, inp.lot_size)

        liq_rows = opt.window(cur.snap.rows, cur.center, cfg.liquidity_strikes) if cur.center else []
        lq = opt.liquidity(liq_rows)
        lp = {"strikes_each_side": cfg.liquidity_strikes}
        sv("atm_spread_pct", lq.median_spread_pct, "median_spread@1", params=lp, null="no two-sided legs")
        sv("atm_min_top_qty", lq.min_top_qty, "min_top_qty@1", params=lp, null="no two-sided legs")
        sv("atm_two_sided_share", lq.two_sided_share if lq.legs else None, "two_sided_share@1", params=lp, null="no legs")
        out["snapshot_age_s"] = v(round(age, 3), "snapshot_age@1", src)

        full = opt.liquidity(cur.snap.rows)
        score = 0.0 if stale else round(full.two_sided_share * 100, 2)
        return score, ("STALE" if stale else "LIVE_OK")

    def _vol(self, out: dict, cur: _ChainCtx, sv, lot_size: int | None) -> None:
        cfg = self.cfg
        f, t, df, atm = cur.forward, cur.t, cur.df, cur.atm
        row = next((r for r in cur.snap.rows if r.strike == atm), None)
        need = "forward, expiry time or ATM quotes missing"
        iv_c = iv_p = None
        if f and t and row is not None:
            pc, pp = opt.leg_price(row.ce), opt.leg_price(row.pe)
            iv_c = black76.implied_vol(pc, f, atm, t, df, True) if pc else None
            iv_p = black76.implied_vol(pp, f, atm, t, df, False) if pp else None
        ivs = [x for x in (iv_c, iv_p) if x is not None]
        atm_iv = sum(ivs) / len(ivs) if ivs else None
        prm = {"rate": cfg.rate}
        sv("atm_iv_b76", None if atm_iv is None else atm_iv * 100, "b76_iv_mid@1", params=prm, null=need)
        dh = [x for x in ((row.ce.iv if row and row.ce else None), (row.pe.iv if row and row.pe else None)) if x]
        dhan = sum(dh) / len(dh) if dh else None
        sv("atm_iv_dhan", dhan, "vendor_iv@1", null="vendor IV missing (quarantined source)")
        sv(
            "atm_iv_diff",
            None if dhan is None or atm_iv is None else dhan - atm_iv * 100,
            "iv_diff@1",
            null="vendor or own IV missing",
        )
        skew = None
        if f and t and cur.step:
            kp, kc = atm - cfg.skew_strikes * cur.step, atm + cfg.skew_strikes * cur.step
            rp = next((r for r in cur.snap.rows if r.strike == kp), None)
            rc = next((r for r in cur.snap.rows if r.strike == kc), None)
            ppx, cpx = opt.leg_price(rp.pe if rp else None), opt.leg_price(rc.ce if rc else None)
            ivp = black76.implied_vol(ppx, f, kp, t, df, False) if ppx else None
            ivc = black76.implied_vol(cpx, f, kc, t, df, True) if cpx else None
            if ivp is not None and ivc is not None:
                skew = (ivp - ivc) * 100
        sv("iv_skew", skew, "b76_skew@1", params={"strikes": cfg.skew_strikes, "rate": cfg.rate}, null="OTM quotes missing")
        g = black76.greeks(f, atm, t, iv_c, df, True, cfg.rate) if (f and t and iv_c) else None
        for name in ("delta", "gamma", "theta", "vega"):
            sv(f"atm_{name}", getattr(g, name) if g else None, "b76_greeks_call@1", params=prm, null=need)
        lot_mult = 1.0 if cfg.oi_unit == "units" else float(lot_size or 0)
        null_g = "oi_unit=contracts requires lot_size" if not lot_mult else None
        gex = opt.gamma_exposure_proxy(cur.snap.rows, f, t, df, cfg.rate, lot_mult) if (f and t and lot_mult) else None
        sv(
            "gamma_exposure_proxy",
            gex,
            "gex_proxy@1",
            params={"oi_unit": cfg.oi_unit, "sign": "calls+ puts-"},
            null=null_g or need,
        )

    # ------------------------------------------------------------------ time

    def _time(self, out: dict, snaps: list[OptionChainSnapshot], inp: FeatureInput, as_of: datetime) -> None:
        cal, cfg, v = inp.calendar, self.cfg, self._v
        today = cal.session_date(as_of)
        open_ts, close_ts = cal.at(today, cal.open), cal.at(today, cal.close)
        trading = cal.is_trading_day(today)
        src = [f"clock:{as_of.isoformat()}", f"calendar:holidays#{len(cal.holidays)}"]
        is_open = cal.is_open(as_of)
        since = (as_of - open_ts).total_seconds() / 60 if is_open else None
        to_close = (close_ts - as_of).total_seconds() / 60 if is_open else None
        out["minutes_since_open"] = v(since, "session_clock@1", src, null_reason=None if is_open else "market closed")
        out["minutes_to_close"] = v(to_close, "session_clock@1", src, null_reason=None if is_open else "market closed")
        out["is_first_window"] = v(
            bool(is_open and since < cfg.first_window_min), "session_clock@1", src, {"min": cfg.first_window_min}
        )  # type: ignore[operator]
        out["is_last_window"] = v(
            bool(is_open and to_close <= cfg.last_window_min), "session_clock@1", src, {"min": cfg.last_window_min}
        )  # type: ignore[operator]
        out["session_open"] = v(is_open, "session_clock@1", src)
        expiry = inp.expiry or (snaps[-1].expiry if snaps else None)
        esrc = src + ([f"expiry:{expiry.isoformat()}"] if expiry else [])
        if expiry is None:
            for n in ("is_expiry_day", "is_day_before_expiry", "dte_calendar", "dte_trading"):
                out[n] = v(None, "expiry_calendar@1", esrc)
            return
        nxt = today + timedelta(days=1)
        while not cal.is_trading_day(nxt):
            nxt += timedelta(days=1)
        tdays, d = 0, today + timedelta(days=1)
        while d <= expiry:
            tdays += cal.is_trading_day(d)
            d += timedelta(days=1)
        out["is_expiry_day"] = v(trading and today == expiry, "expiry_calendar@1", esrc)
        out["is_day_before_expiry"] = v(trading and nxt == expiry, "expiry_calendar@1", esrc)
        out["dte_calendar"] = v((expiry - today).days, "expiry_calendar@1", esrc)
        out["dte_trading"] = v(tdays, "expiry_calendar@1", esrc)

    # ---------------------------------------------------------------- regime

    def _regime(self, out: dict, bars: list[Bar], inp: FeatureInput) -> None:
        cfg, rc, v = self.cfg, self.cfg.regime, self._v
        src = [_bars_id("bars", bars, inp.timeframe_min)]
        e20, e50, a = out["ema_20"].value, out["ema_50"].value, out["adx"].value
        label = None
        if None not in (e20, e50, a) and bars:
            c = bars[-1].close
            if a >= rc.adx_trend_min and e20 > e50 and c > e20:
                label = "TREND_UP"
            elif a >= rc.adx_trend_min and e20 < e50 and c < e20:
                label = "TREND_DOWN"
            else:
                label = "RANGE"
        out["trend_regime"] = v(label, "trend_regime@1", src, {"adx_min": rc.adx_trend_min}, warm=bool(bars))
        at = ta.atr(bars, cfg.atr_period)
        hist = [x / b.close * 100 for x, b in zip(at, bars, strict=True) if x is not None and b.close][
            -rc.vol_percentile_window :
        ]
        pct = ta.percentile_rank(hist, hist[-1]) if len(hist) >= 20 else None
        vp = {"window": rc.vol_percentile_window, "high": rc.vol_high_pct, "low": rc.vol_low_pct}
        out["atr_pct_percentile"] = v(pct, "percentile_rank@1", src, vp, warm=bool(bars))
        vl = None if pct is None else "HIGH" if pct >= rc.vol_high_pct else "LOW" if pct <= rc.vol_low_pct else "NORMAL"
        out["vol_regime"] = v(vl, "vol_regime@1", src, vp, warm=bool(bars))
