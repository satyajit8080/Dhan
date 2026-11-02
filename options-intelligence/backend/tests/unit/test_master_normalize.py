from datetime import UTC, date, datetime

import pytest

from doi.core.errors import InstrumentError, SchemaError
from doi.data.dhan.normalize import normalize_candles, normalize_expiry_list, normalize_option_chain, normalize_rolling_option
from doi.data.instruments.master import InstrumentMaster
from doi.intelligence.strategy_configs.loader import UNIVERSE
from tests.fixtures import E1, E2, chain_payload, master_csv

EXCH = {"NIFTY": "NSE", "BANKNIFTY": "NSE", "FINNIFTY": "NSE", "SENSEX": "BSE", "BANKEX": "BSE"}


def test_all_five_resolve_from_master():
    m = InstrumentMaster.parse(master_csv())
    for key in UNIVERSE:
        ref = m.resolve_underlying(key, EXCH[key], key)
        assert ref.master_version == m.version.version and len(ref.master_version) == 64
        assert [p.expiry for p in ref.expiries] == [E1, E2]
        assert ref.derivative_segment == ("BSE_FNO" if EXCH[key] == "BSE" else "NSE_FNO")
    n = m.resolve_underlying("NIFTY", "NSE", "NIFTY")
    assert (n.security_id, n.expiries[0].lot_size, n.expiries[0].strike_step) == ("9013", 75, 50)
    assert m.resolve_underlying("SENSEX", "BSE", "SENSEX").expiries[0].strike_step == 100


def test_version_changes_with_content():
    assert InstrumentMaster.parse(master_csv()).version.version != InstrumentMaster.parse(master_csv() + "\n").version.version


def test_expired_contracts_filtered_by_today():
    ref = InstrumentMaster.parse(master_csv()).resolve_underlying("NIFTY", "NSE", "NIFTY", today=date(2026, 11, 11))
    assert [p.expiry for p in ref.expiries] == [E2]


def test_lot_size_change_detected_not_reused():
    bad = master_csv(extra=[f'NSE,D,999999,NA,OPTIDX,9013,NIFTY,x,"x",OP,NA,50,{E1} 14:30:00,24500,CE,0.05,W'])
    with pytest.raises(InstrumentError, match="lot size"):
        InstrumentMaster.parse(bad).resolve_underlying("NIFTY", "NSE", "NIFTY")


def test_ambiguous_underlying_id_rejected():
    bad = master_csv(extra=[f'NSE,D,999998,NA,OPTIDX,7777,NIFTY,x,"x",OP,NA,75,{E1} 14:30:00,24500,CE,0.05,W'])
    with pytest.raises(InstrumentError, match="ambiguous"):
        InstrumentMaster.parse(bad).resolve_underlying("NIFTY", "NSE", "NIFTY")


def test_contract_lookup_and_missing():
    m = InstrumentMaster.parse(master_csv())
    c = m.contract("NSE", "NIFTY", E1, 24050, "CE")
    assert c.option_type == "CE" and c.lot_size == 75 and c.exchange_segment == "NSE_FNO"
    with pytest.raises(InstrumentError, match="no contract"):
        m.contract("NSE", "NIFTY", E1, 24025, "CE")


def test_legacy_column_names_supported():
    legacy = (
        "SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,UNDERLYING_SECURITY_ID,SM_SYMBOL_NAME,"
        "SEM_TRADING_SYMBOL,SEM_EXPIRY_DATE,SEM_STRIKE_PRICE,SEM_OPTION_TYPE,SEM_LOT_UNITS,SEM_TICK_SIZE\n"
        "NSE,I,13,INDEX,13,NIFTY,Nifty 50,,,,1,0.05\n"
        "NSE,D,1,OPTIDX,26000,NIFTY,a,2026-11-10,24000,CE,75,0.05\nNSE,D,2,OPTIDX,26000,NIFTY,b,2026-11-10,24050,CE,75,0.05\n"
    )
    ref = InstrumentMaster.parse(legacy).resolve_underlying("NIFTY", "NSE", "NIFTY")
    assert ref.expiries[0].strike_step == 50
    assert ref.security_id == "13"  # the IDX_I index id, not the options' underlying code 26000 (live master layout)


def test_missing_columns_fail_loudly():
    with pytest.raises(InstrumentError, match="missing columns"):
        InstrumentMaster.parse("A,B,C\n1,2,3\n")


def test_option_chain_normalization():
    now = datetime(2026, 11, 2, 4, 0, tzinfo=UTC)
    s = normalize_option_chain(
        chain_payload(), underlying_key="NIFTY", expiry=E1, request_key="k", requested_at=now, received_at=now, master_version="v"
    )
    assert [r.strike for r in s.rows] == [24000, 24050] and s.underlying_ltp == 24012.4
    pe = s.rows[1].pe
    assert pe.bid is None and pe.iv is None and not pe.two_sided  # zero bid / zero IV are "missing", never prices
    assert s.rows[0].ce.change_oi == 1000 and s.rows[0].ce.two_sided


def test_option_chain_schema_errors():
    kw = dict(
        underlying_key="N",
        expiry=E1,
        request_key="k",
        requested_at=datetime.now(UTC),
        received_at=datetime.now(UTC),
        master_version="v",
    )
    with pytest.raises(SchemaError):
        normalize_option_chain({"status": "success"}, **kw)
    with pytest.raises(SchemaError):
        normalize_option_chain({"data": {"oc": {"abc": {}}}}, **kw)


def test_expiry_list():
    assert normalize_expiry_list({"data": ["2026-11-24", "2026-11-10"]}) == (E1, E2)
    with pytest.raises(SchemaError):
        normalize_expiry_list({"data": "x"})


def test_rolling_option_and_candles():
    ts = [1762054500, 1762054560]
    side = {
        "timestamp": ts,
        "open": [10, 11],
        "high": [12, 12],
        "low": [9, 10],
        "close": [11, 11.5],
        "oi": [5, 6],
        "spot": [24000, 24001],
        "strike": [24000, 24000],
        "iv": [12, 12],
        "volume": [1, 2],
    }
    bars = normalize_rolling_option({"data": {"ce": side, "pe": None}}, "CALL")
    assert len(bars) == 2 and bars[0].ts.tzinfo is UTC and bars[1].spot == 24001
    assert normalize_rolling_option({"data": {"ce": side, "pe": None}}, "PUT") == ()
    with pytest.raises(SchemaError, match="length"):
        normalize_rolling_option({"data": {"ce": {**side, "oi": [1]}}}, "CALL")
    candles = normalize_candles(
        {"timestamp": ts, "open": [1, 2], "high": [2, 3], "low": [1, 1], "close": [2, 2], "volume": [5, 5]}
    )
    assert candles[1].close == 2 and candles[0].oi is None


def test_missing_index_row_fails_loudly():
    no_index = "\n".join(line for line in master_csv().splitlines() if ",I," not in line) + "\n"
    with pytest.raises(InstrumentError, match="IDX_I INDEX row"):
        InstrumentMaster.parse(no_index).resolve_underlying("NIFTY", "NSE", "NIFTY")
