"""
Local read-only scanner: Dhan data -> validation -> Phase-4 engine -> refresh table.

One cycle (`run_once`) mirrors the TypeScript snapshot order:
  expiry list -> next weekly after today (RULES.md §6)
  -> option chain FIRST -> futures quote immediately after (skew rule)
  -> today's 5-minute SENSEX index candles
  -> sensex.scan.build_scan -> refresh_table (existing engine, unchanged)
and returns one timestamped record. `run_loop` repeats it on an interval and
stops at the session stop time (default 15:30 IST), on Ctrl-C / SIGTERM, on
`max_scans`, or on an authentication / data-plan failure.

Never: orders, position exits, token generation or renewal, invented
thresholds, CE/PE signals, or a table built from data that failed validation.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timezone

from .dhan_client import AuthError, DhanClientError, PlanError, Redactor
from .expiries import ExpirySelectionError, next_after
from .instruments import InstrumentResolutionError, explicit_future, select_future
from .normalize import to_candles
from .scan import BLANK_TABLE, build_scan, run_refresh
from .session import DEFAULT_STOP, IST, session_state
from .validation import (DataValidationError, snapshot_fingerprint, validate_candles, validate_chain,
                         validate_expiry_list, validate_quote)

SENSEX_SCRIP, SENSEX_SEG, FUT_SEG = 51, "IDX_I", "BSE_FNO"


class JsonLogger:
    """One JSON object per line, every string passed through the redactor."""

    def __init__(self, redactor: Redactor, stream=None, path: str | None = None, clock=None):
        self.redact, self.stream, self.path = redactor, stream or sys.stderr, path
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def __call__(self, level: str, event: str, **fields):
        rec = {"ts": self.clock().astimezone(IST).isoformat(timespec="seconds"), "level": level, "event": event}
        rec.update(fields)
        line = self.redact(json.dumps(rec, default=str, ensure_ascii=False))
        self.stream.write(line + "\n")
        self.stream.flush()
        if self.path:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")


@dataclass
class ScannerConfig:
    strikes: list
    interval_s: float = 60.0
    stop_time: dtime = DEFAULT_STOP
    holidays: frozenset = frozenset()
    futures_security_id: str | None = None
    futures_expiry: str | None = None
    instrument_contracts: list | None = None   # from a VERIFIED instrument master (instruments.parse_futures)
    candle_interval: int = 5
    risk_free_rate: float = 0.065
    max_snapshot_skew_ms: float = 3000
    max_scans: int | None = None
    ignore_session: bool = False               # replay of recorded data only; never for live scans
    min_interval_s: float = 10.0


@dataclass
class ScanState:
    scans: int = 0
    last_fingerprint: str | None = None
    stop_reason: str | None = None
    history: list = field(default_factory=list)


class Scanner:
    def __init__(self, client, config: ScannerConfig, log, clock=None, sleep=time.sleep):
        if not config.strikes:
            raise ValueError("strikes are required: ATM±2 selection is not defined yet (docs/PHASE4_PORT_PLAN.md P1)")
        if config.interval_s < config.min_interval_s:
            raise ValueError("interval must be >= %ss (Dhan option-chain limit is 1 per 3 s)" % config.min_interval_s)
        self.client, self.cfg, self.log = client, config, log
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep
        self.state = ScanState()
        self.stop_event = threading.Event()

    # ----------------------------------------------------------------- one scan
    def _future(self, option_expiry: date, today: date):
        if self.cfg.instrument_contracts is not None:
            return select_future(self.cfg.instrument_contracts, option_expiry, today)
        if not self.cfg.futures_security_id or not self.cfg.futures_expiry:
            raise InstrumentResolutionError(
                "No futures contract: pass --futures-security-id and --futures-expiry (automatic lookup is "
                "BLOCKED until the instrument-master header is verified).")
        return explicit_future(self.cfg.futures_security_id, self.cfg.futures_expiry, today, option_expiry)

    def run_once(self) -> dict:
        now = self.clock()
        ist = now.astimezone(IST)
        self.state.scans += 1
        rec = {"scan": self.state.scans, "startedIst": ist.isoformat(timespec="seconds"), "status": None,
               "warnings": [], "envelopes": {}, "table": BLANK_TABLE}
        try:
            st = session_state(now, self.cfg.holidays, self.cfg.stop_time)
            if not st.open and not self.cfg.ignore_session:
                rec.update(status="SKIPPED", reason=st.reason)
                return self._finish(rec)
            today = ist.date()

            exp_resp = self.client.expiry_list(SENSEX_SCRIP, SENSEX_SEG)
            rec["envelopes"]["expirylist"] = exp_resp.envelope
            expiries = validate_expiry_list(exp_resp.payload)
            expiry = next_after(expiries, today.isoformat(), "%s:%d" % (SENSEX_SEG, SENSEX_SCRIP))
            rec["expiry"] = expiry
            fut = self._future(date.fromisoformat(expiry), today)
            rec["futures"] = {"securityId": fut.security_id, "expiry": fut.expiry.isoformat()}

            chain = self.client.option_chain(expiry, SENSEX_SCRIP, SENSEX_SEG)       # chain FIRST
            quote = self.client.quote({FUT_SEG: [int(fut.security_id)]})               # futures right after
            rec["envelopes"].update(optionchain=chain.envelope, quote=quote.envelope)
            rec["warnings"] += validate_chain(chain.payload)
            fp = snapshot_fingerprint(chain.payload)
            if fp == self.state.last_fingerprint:
                rec["warnings"].append("DUPLICATE_SNAPSHOT: option chain identical to the previous scan "
                                       "(feed may be stale, or the market is closed/halted)")
            self.state.last_fingerprint = fp
            rec["chainFingerprint"] = fp
            try:
                raw_fut = validate_quote(quote.payload, FUT_SEG, fut.security_id)
            except DataValidationError as e:
                raw_fut = None  # the gate then blocks with NO_FUTURES_QUOTE — never substitutes the index
                rec["warnings"].append(e.code + ": " + e.message)

            day = today.isoformat()
            candles = []
            try:
                cnd = self.client.intraday_candles(str(SENSEX_SCRIP), SENSEX_SEG, "INDEX", self.cfg.candle_interval,
                                                   day + " 09:15:00", day + " 15:30:00")
                rec["envelopes"]["intraday"] = cnd.envelope
                rec["warnings"] += validate_candles(cnd.payload)
                candles = to_candles(cnd.payload)
            except (AuthError, PlanError):
                raise
            except DataValidationError as e:
                rec["warnings"].append(e.code + ": " + e.message)
            except DhanClientError as e:
                # SKILL.md: a dead candle endpoint is not a reason to fail the
                # scan. Without candles no level can be derived, so rows show
                # NO_LEVEL — nothing is estimated.
                rec["warnings"].append("CANDLES_UNAVAILABLE: %s: %s" % (type(e).__name__, e))
            if not candles:
                rec["warnings"].append("NO_CANDLES: no index candles for today (holiday, pre-open, or feed issue); "
                                       "levels cannot be derived")

            scan = build_scan(raw_chain=chain.payload, raw_futures=raw_fut, candles=candles, expiry=expiry,
                              strikes=self.cfg.strikes, chain_received_ms=chain.received_at_ms,
                              futures_received_ms=quote.received_at_ms, futures_security_id=fut.security_id,
                              futures_expiry=fut.expiry.isoformat(), risk_free_rate=self.cfg.risk_free_rate,
                              max_snapshot_skew_ms=self.cfg.max_snapshot_skew_ms)
            rows, table = run_refresh(scan, ist)
            rec.update(status=scan["status"], table=table, scanInputs=scan)
            if scan["status"] == "OK":
                rec.update(forward=scan["forward"], atmStrike=scan["atmStrike"],
                           rowStatuses=sorted({r.status for r in rows}) if rows else [],
                           excludedLegs=scan["excludedLegs"])
            else:
                rec["reason"] = " | ".join(scan.get("reasons", []))
        except (AuthError, PlanError) as e:
            rec.update(status="AUTH_FAILED" if isinstance(e, AuthError) else "PLAN_MISSING", reason=str(e))
            self.state.stop_reason = rec["status"]
            self.stop_event.set()
        except InstrumentResolutionError as e:
            rec.update(status="CONFIG_BLOCKED", reason=str(e))
        except (DataValidationError, ExpirySelectionError) as e:
            rec.update(status="INVALID_DATA", reason=str(e))
        except DhanClientError as e:
            rec.update(status="ERROR", reason="%s: %s" % (type(e).__name__, e))
        return self._finish(rec)

    def _finish(self, rec: dict) -> dict:
        rec["finishedIst"] = self.clock().astimezone(IST).isoformat(timespec="seconds")
        self.state.history.append(rec["status"])
        self.log("info" if rec["status"] in ("OK", "SKIPPED") else "warn", "scan",
                 **{k: v for k, v in rec.items() if k != "scanInputs"})
        return rec

    # ------------------------------------------------------------------- loop
    def request_stop(self, reason: str = "SIGNAL") -> None:
        self.state.stop_reason = self.state.stop_reason or reason
        self.stop_event.set()

    def run_loop(self, on_record=None) -> str:
        """Run until stop time, a stop request, max_scans, or an auth failure.
        Returns the stop reason. Must be started explicitly by the user."""
        self.log("info", "scanner_start", interval_s=self.cfg.interval_s, stop=self.cfg.stop_time.isoformat(),
                 strikes=self.cfg.strikes, max_scans=self.cfg.max_scans)
        while not self.stop_event.is_set():
            st = session_state(self.clock(), self.cfg.holidays, self.cfg.stop_time)
            if not st.open and not self.cfg.ignore_session:
                self.state.stop_reason = self.state.stop_reason or "SESSION_" + st.reason
                break
            tick = self.clock()   # same clock as the session boundary, so tests and live runs agree
            rec = self.run_once()
            if on_record:
                on_record(rec)
            if self.stop_event.is_set():
                break
            if self.cfg.max_scans is not None and self.state.scans >= self.cfg.max_scans:
                self.state.stop_reason = "MAX_SCANS"
                break
            remaining = self.cfg.interval_s - (self.clock() - tick).total_seconds()
            while remaining > 0 and not self.stop_event.is_set():   # sleep in slices: Ctrl-C is prompt
                step = min(1.0, remaining)
                self.sleep(step)
                remaining -= step
        reason = self.state.stop_reason or "STOPPED"
        self.log("info", "scanner_stop", reason=reason, scans=self.state.scans, statuses=self.state.history)
        return reason
