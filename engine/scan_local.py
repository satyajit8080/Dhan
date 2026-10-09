#!/usr/bin/env python3
"""
Local, READ-ONLY SENSEX refresh scanner.  Nothing runs until you invoke it.

  # offline demo on labelled MOCK data (no network, no credentials):
  python3 scan_local.py --mock

  # one read-only call to check your token and data plan (prints no secrets):
  python3 scan_local.py --profile-check

  # one live scan, then exit:
  python3 scan_local.py --once --strikes 74500,74600,74700,74800,74900 \
      --futures-security-id <id> --futures-expiry YYYY-MM-DD

  # repeat every 60 s until 15:30 IST (Ctrl-C stops cleanly):
  python3 scan_local.py --interval 60 --strikes ... --futures-security-id ... --futures-expiry ...

Credentials come ONLY from your local environment: DHAN_CLIENT_ID plus
DHAN_ACCESS_TOKEN or DHAN_TOKEN_FILE (see docs/PHASE5_DATA_CLIENT_DESIGN.md §7).
Never pass them on the command line. No orders, no login, no token renewal.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from datetime import datetime, time as dtime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sensex.dhan_client import AuthError, DhanClient, DhanClientError, Redactor  # noqa: E402
from sensex.instruments import InstrumentResolutionError, parse_futures  # noqa: E402
from sensex.localio import (RecordingTransport, credentials_from_environment, file_sink, load_holidays,  # noqa: E402
                            load_mapping)
from sensex.scanner import JsonLogger, Scanner, ScannerConfig  # noqa: E402
from sensex.session import IST  # noqa: E402


def _strikes(text: str) -> list:
    try:
        vals = [float(x) for x in text.split(",") if x.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError("--strikes must be comma-separated numbers") from None
    if not vals:
        raise argparse.ArgumentTypeError("--strikes is empty")
    return vals


def _hhmm(text: str) -> dtime:
    return dtime.fromisoformat(text)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Local read-only SENSEX refresh scanner (no orders).")
    p.add_argument("--mock", action="store_true", help="offline run on labelled MOCK fixtures; no network")
    p.add_argument("--profile-check", action="store_true", help="one read-only GET /profile, then exit")
    p.add_argument("--once", action="store_true", help="run a single scan and exit")
    p.add_argument("--strikes", type=_strikes, help="explicit strikes for the table (ATM±2 rule not yet defined)")
    p.add_argument("--interval", type=float, default=60.0, help="seconds between scans (min 10)")
    p.add_argument("--stop-at", type=_hhmm, default=dtime(15, 30), help="IST stop time, default 15:30")
    p.add_argument("--max-scans", type=int, help="stop after N scans")
    p.add_argument("--futures-security-id", help="BSE_FNO security id of the SENSEX future used for the gate")
    p.add_argument("--futures-expiry", help="its expiry, YYYY-MM-DD (refused once expired)")
    p.add_argument("--instrument-mapping", help="VERIFIED mapping JSON (see design doc §4); BLOCKED until verified")
    p.add_argument("--instrument-csv", help="local copy of the Dhan instrument master to resolve the future from")
    p.add_argument("--holidays", help="file of YYYY-MM-DD exchange holidays (you maintain it)")
    p.add_argument("--log-file", help="JSON-lines log path (default scan-logs/scan-<date>.jsonl)")
    p.add_argument("--record", metavar="DIR", help="save raw market-data bodies per scan for offline replay "
                                                   "(no headers, no credentials)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    redactor = Redactor()
    if args.mock:
        from sensex.mock_dhan import mock_scanner  # local import: mock fixtures only
        scanner, log = mock_scanner(redactor)
        rec = scanner.run_once()
        print("[MOCK DATA — not market data] %s status=%s" % (rec["startedIst"], rec["status"]))
        print(rec["table"])
        return 0 if rec["status"] == "OK" else 1

    try:
        creds = credentials_from_environment()
    except (AuthError, OSError) as e:
        print("credentials: %s" % type(e).__name__ + (": " + str(e) if isinstance(e, AuthError) else ""), file=sys.stderr)
        return 2
    redactor.register(creds.access_token, creds.client_id)
    log_path = args.log_file or "scan-logs/scan-%s.jsonl" % datetime.now(IST).strftime("%Y%m%d")
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    log = JsonLogger(redactor, sink=file_sink(log_path))
    recorder = None
    if args.record:
        from sensex.dhan_client import UrllibTransport
        recorder = RecordingTransport(UrllibTransport(), args.record)
    client = DhanClient(creds, transport=recorder, redactor=redactor, logger=log)

    exp = creds.token_expiry_ms()
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    if exp is not None and exp <= now_ms:
        log("error", "token_expired", hours_ago=round((now_ms - exp) / 3.6e6, 2))
        return 2
    if exp is not None:
        log("info", "token_status", hours_left=round((exp - now_ms) / 3.6e6, 2))

    if args.profile_check:
        try:
            r = client.profile()
        except DhanClientError as e:
            log("error", "profile_check_failed", error=type(e).__name__, error_code=e.error_code, http_status=e.http_status)
            return 2
        body = r.payload if isinstance(r.payload, dict) else {}
        # Report presence only — never the values (they identify the account).
        print(json.dumps({"profile_ok": True, "envelope": r.envelope,
                          "fields_present": sorted(k for k in ("dataPlan", "dataValidity", "tokenValidity", "activeSegment") if k in body)}))
        return 0

    if not args.strikes:
        print("--strikes is required (the ATM±2 rule is not defined yet).", file=sys.stderr)
        return 2
    contracts = None
    if args.instrument_mapping or args.instrument_csv:
        try:
            mapping = load_mapping(args.instrument_mapping)
            contracts, report = parse_futures(Path(args.instrument_csv).read_text(encoding="utf-8"), mapping)
            log("info", "instrument_master", matching=report.rows_matching, malformed_expiry=report.malformed_expiry,
                malformed_lot=report.malformed_lot, verified_from=mapping.verified_from)
        except (InstrumentResolutionError, OSError, TypeError) as e:
            log("error", "instrument_master_blocked", error=type(e).__name__, message=str(e))
            return 2
    cfg = ScannerConfig(strikes=args.strikes, interval_s=args.interval, stop_time=args.stop_at,
                        holidays=load_holidays(args.holidays), futures_security_id=args.futures_security_id,
                        futures_expiry=args.futures_expiry, instrument_contracts=contracts,
                        max_scans=1 if args.once else args.max_scans)
    scanner = Scanner(client, cfg, log)
    if recorder is not None:
        orig_run_once = scanner.run_once

        def recorded_run_once():
            recorder.begin_scan(scanner.state.scans + 1, {
                "strikes": cfg.strikes, "futuresSecurityId": cfg.futures_security_id,
                "futuresExpiry": cfg.futures_expiry, "clockUtc": scanner.clock().isoformat()})
            rec = orig_run_once()
            recorder.flush()
            return rec
        scanner.run_once = recorded_run_once
    signal.signal(signal.SIGINT, lambda *_: scanner.request_stop("SIGINT"))
    signal.signal(signal.SIGTERM, lambda *_: scanner.request_stop("SIGTERM"))

    def show(rec):
        print("\n[%s] scan #%d status=%s expiry=%s atm=%s" % (rec["startedIst"], rec["scan"], rec["status"],
                                                             rec.get("expiry"), rec.get("atmStrike")))
        if rec.get("reason"):
            print("reason: " + redactor(rec["reason"]))
        print(rec["table"])

    reason = scanner.run_loop(on_record=show)
    print("\nstopped: %s after %d scan(s); log: %s" % (reason, scanner.state.scans, os.path.abspath(log_path)))
    return 2 if reason in ("AUTH_FAILED", "PLAN_MISSING") else 0


if __name__ == "__main__":
    sys.exit(main())
