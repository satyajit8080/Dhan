"""Instrument parser/selector (SYNTHETIC format), market session, input validation."""

import json
import os
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone

from sensex.instruments import (DuplicateContractError, FuturesContract, InstrumentMapping, MappingMismatchError,
                                MappingMissingError, NoEligibleContractError, explicit_future, parse_futures,
                                select_future)
from sensex.localio import load_holidays, load_mapping
from sensex.session import IST, session_state
from sensex.validation import (DataValidationError, snapshot_fingerprint, validate_candles, validate_chain,
                               validate_expiry_list, validate_quote)

# SYNTHETIC test format. Column names and values are deliberately NOT Dhan's
# (the real header is UNVERIFIED); they only exercise the parser logic.
SYN = InstrumentMapping(
    columns={"exchange": "T_EXCH", "security_id": "T_ID", "instrument": "T_KIND", "underlying": "T_UND",
             "expiry": "T_EXP", "lot_size": "T_LOT"},
    values={"exchange": "XCH", "instrument": "FUTX", "underlying": "IDXA"},
    expiry_format="%d-%m-%Y", verified_from="synthetic unit-test format")
HEADER = "T_EXCH,T_ID,T_KIND,T_UND,T_EXP,T_LOT,EXTRA"


def csv_of(*rows):
    return "\n".join([HEADER] + list(rows)) + "\n"


class InstrumentParsing(unittest.TestCase):
    def test_parse_filters_and_counts_malformed(self):
        text = csv_of("XCH,1001,FUTX,IDXA,29-10-2026,20,x", "XCH,1002,FUTX,IDXA,26-11-2026,20,x",
                      "XCH,1003,OPTX,IDXA,29-10-2026,20,x",      # other instrument
                      "YYY,1004,FUTX,IDXA,29-10-2026,20,x",      # other exchange
                      "XCH,1005,FUTX,IDXB,29-10-2026,20,x",      # other underlying
                      "XCH,10x6,FUTX,IDXA,29-10-2026,20,x",      # malformed id
                      "XCH,1007,FUTX,IDXA,2026-10-29,20,x",      # malformed date for this format
                      "XCH,1008,FUTX,IDXA,31-02-2026,20,x",      # impossible date
                      "XCH,1009,FUTX,IDXA,31-12-2026,abc,x",     # malformed lot: kept, lot None
                      "", "xch,1010,futx,idxa,31-01-2027,20")    # case-insensitive match, short row ok
        contracts, rep = parse_futures(text, SYN)
        self.assertEqual([c.security_id for c in contracts], ["1001", "1002", "1009", "1010"])
        self.assertEqual((rep.malformed_id, rep.malformed_expiry, rep.malformed_lot), (1, 2, 1))
        self.assertIsNone(contracts[2].lot_size)
        self.assertEqual(contracts[0].lot_size, 20)

    def test_header_mismatch_is_refused(self):
        with self.assertRaises(MappingMismatchError):
            parse_futures("A,B,C\n1,2,3\n", SYN)
        with self.assertRaises(MappingMismatchError):
            parse_futures("", SYN)

    def test_mapping_must_be_explicit_and_verified(self):
        with self.assertRaises(MappingMissingError):
            load_mapping(None)
        for bad in [{}, {"columns": SYN.columns, "values": SYN.values, "expiry_format": "%d-%m-%Y"},
                    {"columns": {}, "values": SYN.values, "expiry_format": "x", "verified_from": "y"}]:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                json.dump(bad, f)
            try:
                with self.assertRaises(MappingMissingError):
                    load_mapping(f.name)
            finally:
                os.unlink(f.name)


def fc(sid, d):
    return FuturesContract(sid, date.fromisoformat(d), 20, "XCH", d)


class FuturesSelection(unittest.TestCase):
    C = [fc("1", "2026-09-24"), fc("2", "2026-10-29"), fc("3", "2026-11-26")]

    def test_expiry_day_option_uses_same_day_future(self):
        self.assertEqual(select_future(self.C, date(2026, 10, 29), date(2026, 10, 29)).security_id, "2")

    def test_next_contract_after_option_expiry(self):
        self.assertEqual(select_future(self.C, date(2026, 10, 30), date(2026, 10, 9)).security_id, "3")

    def test_expired_contracts_never_eligible(self):
        self.assertEqual(select_future(self.C, date(2026, 9, 1), date(2026, 9, 25)).security_id, "2")

    def test_missing_contract(self):
        with self.assertRaises(NoEligibleContractError):
            select_future(self.C, date(2026, 12, 3), date(2026, 11, 27))
        with self.assertRaises(NoEligibleContractError):
            select_future([], date(2026, 10, 1), date(2026, 10, 1))

    def test_duplicate_matches_refused(self):
        with self.assertRaises(DuplicateContractError):
            select_future(self.C + [fc("9", "2026-10-29")], date(2026, 10, 15), date(2026, 10, 9))
        # the same id listed twice is not ambiguous
        self.assertEqual(select_future(self.C + [fc("2", "2026-10-29")], date(2026, 10, 15), date(2026, 10, 9)).security_id, "2")

    def test_future_dates_far_ahead(self):
        far = [fc("7", "2027-03-25"), fc("8", "2027-01-28")]
        self.assertEqual(select_future(far, date(2026, 12, 31), date(2026, 12, 1)).security_id, "8")

    def test_explicit_contract_validated(self):
        self.assertEqual(explicit_future("864571", "2026-10-29", date(2026, 10, 9), date(2026, 10, 15)).security_id, "864571")
        with self.assertRaises(NoEligibleContractError):   # expired
            explicit_future("844615", "2026-09-24", date(2026, 10, 9), date(2026, 10, 15))
        with self.assertRaises(NoEligibleContractError):   # expires before the option
            explicit_future("864571", "2026-10-29", date(2026, 10, 9), date(2026, 11, 5))
        with self.assertRaises(Exception):
            explicit_future("abc", "2026-10-29", date(2026, 10, 9), date(2026, 10, 15))


def ist(y, m, d, hh, mm, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=IST)


class Session(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(session_state(ist(2026, 10, 9, 9, 14, 59)).reason, "BEFORE_OPEN")
        self.assertTrue(session_state(ist(2026, 10, 9, 9, 15)).open)
        self.assertTrue(session_state(ist(2026, 10, 9, 15, 29, 59)).open)
        self.assertEqual(session_state(ist(2026, 10, 9, 15, 30)).reason, "AFTER_STOP")
        self.assertEqual(session_state(ist(2026, 10, 10, 11, 0)).reason, "WEEKEND")   # Saturday
        self.assertEqual(session_state(ist(2026, 10, 11, 11, 0)).reason, "WEEKEND")   # Sunday
        self.assertEqual(session_state(ist(2026, 10, 9, 15, 0), stop=time(14, 50)).reason, "AFTER_STOP")

    def test_utc_clock_converted(self):
        self.assertTrue(session_state(datetime(2026, 10, 9, 4, 0, tzinfo=timezone.utc)).open)   # 09:30 IST
        self.assertFalse(session_state(datetime(2026, 10, 9, 10, 1, tzinfo=timezone.utc)).open)  # 15:31 IST
        with self.assertRaises(ValueError):
            session_state(datetime(2026, 10, 9, 10, 0))   # naive clock refused

    def test_holiday_file(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("# user-maintained\n2026-10-20  # example entry, not a verified holiday\n\n")
        try:
            h = load_holidays(f.name)
            self.assertEqual(session_state(ist(2026, 10, 20, 11, 0), h).reason, "HOLIDAY")
        finally:
            os.unlink(f.name)
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("20/10/2026\n")
        try:
            with self.assertRaises(ValueError):
                load_holidays(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual(load_holidays(None), frozenset())


class Validation(unittest.TestCase):
    def test_expiry_list(self):
        self.assertEqual(validate_expiry_list(["2026-10-15", "N/A", "2026-10-08"]), ["2026-10-08", "2026-10-15"])
        for bad in [{}, [], ["N/A"]]:
            with self.assertRaises(DataValidationError):
                validate_expiry_list(bad)

    def test_chain(self):
        good = {"last_price": 1, "oc": {"100": {"ce": {"last_price": 1}, "pe": {"last_price": 2}},
                                        "200": {"ce": {"last_price": 1}, "pe": {"last_price": 2}}}}
        self.assertEqual(validate_chain(good), [])
        no_ltp = dict(good, last_price=None)
        self.assertTrue(any("NO_INDEX_LTP" in w for w in validate_chain(no_ltp)))
        with_bad_key = {"last_price": 1, "oc": dict(good["oc"], abc={})}
        self.assertTrue(any("BAD_STRIKE_KEYS" in w for w in validate_chain(with_bad_key)))
        for bad, code in [([], "CHAIN_SCHEMA"), ({"oc": {}}, "CHAIN_EMPTY"), ({"oc": []}, "CHAIN_EMPTY"),
                          ({"oc": {"100": {"ce": {"last_price": 1}}}}, "CHAIN_INSUFFICIENT_PAIRS"),
                          ({"oc": {"100": {"ce": {"last_price": "x"}, "pe": {"last_price": 1}},
                                   "200": {"ce": {"last_price": 1}, "pe": {"last_price": 2}}}}, "CHAIN_INSUFFICIENT_PAIRS")]:
            with self.assertRaises(DataValidationError) as cm:
                validate_chain(bad)
            self.assertEqual(cm.exception.code, code)

    def test_quote(self):
        p = {"BSE_FNO": {"844615": {"last_price": 74656.75}}}
        self.assertEqual(validate_quote(p, "BSE_FNO", 844615)["last_price"], 74656.75)
        for bad, code in [([], "QUOTE_SCHEMA"), ({}, "QUOTE_MISSING_SEGMENT"), ({"BSE_FNO": {}}, "QUOTE_MISSING_INSTRUMENT"),
                          ({"BSE_FNO": {"844615": {"last_price": None}}}, "QUOTE_NO_LTP")]:
            with self.assertRaises(DataValidationError) as cm:
                validate_quote(bad, "BSE_FNO", 844615)
            self.assertEqual(cm.exception.code, code)

    def test_candles(self):
        ok = {"timestamp": [1, 2], "open": [1, 2], "high": [1, 2], "low": [1, 2], "close": [1, 2]}
        self.assertEqual(validate_candles(ok), [])
        self.assertTrue(validate_candles(dict(ok, close=[1])))       # ragged -> warning
        self.assertTrue(validate_candles(dict(ok, timestamp=[2, 1])))  # order -> warning
        with self.assertRaises(DataValidationError):
            validate_candles({"timestamp": [1]})

    def test_fingerprint_is_order_independent(self):
        self.assertEqual(snapshot_fingerprint({"a": 1, "b": 2}), snapshot_fingerprint({"b": 2, "a": 1}))
        self.assertNotEqual(snapshot_fingerprint({"a": 1}), snapshot_fingerprint({"a": 2}))


if __name__ == "__main__":
    unittest.main()
