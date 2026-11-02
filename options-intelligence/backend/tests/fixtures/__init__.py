"""Synthetic fixtures. Security ids here are fake and exist only for tests."""

from datetime import date, timedelta

HEADER = (
    "EXCH_ID,SEGMENT,SECURITY_ID,ISIN,INSTRUMENT,UNDERLYING_SECURITY_ID,UNDERLYING_SYMBOL,SYMBOL_NAME,"
    "DISPLAY_NAME,INSTRUMENT_TYPE,SERIES,LOT_SIZE,SM_EXPIRY_DATE,STRIKE_PRICE,OPTION_TYPE,TICK_SIZE,EXPIRY_FLAG"
)

E1 = date(2026, 11, 10)
E2 = date(2026, 11, 24)


def _opt(exch, sid, und_id, sym, exp, strike, ot, lot):
    return f'{exch},D,{sid},NA,OPTIDX,{und_id},{sym},{sym}-x,"{sym} {exp:%d %b} {strike:g} {ot}",OP,NA,{lot},{exp} 14:30:00,{strike},{ot},0.05,W'


def master_csv(extra: list[str] | None = None) -> str:
    rows = [HEADER, "NSE,I,9013,NA,INDEX,,NIFTY,NIFTY,Nifty 50,INDEX,NA,1,,,,0.05,"]
    sid = 100000
    for sym, exch, uid, lot, step, mid in (
        ("NIFTY", "NSE", "9013", 75, 50, 24000),
        ("BANKNIFTY", "NSE", "9025", 35, 100, 52000),
        ("FINNIFTY", "NSE", "9027", 65, 50, 23500),
        ("SENSEX", "BSE", "9051", 20, 100, 80000),
        ("BANKEX", "BSE", "9069", 30, 100, 60000),
    ):
        for exp in (E1, E2):
            for k in range(-3, 4):
                for ot in ("CE", "PE"):
                    sid += 1
                    rows.append(_opt(exch, sid, uid, sym, exp, mid + k * step, ot, lot))
    rows += extra or []
    return "\n".join(rows) + "\n"


def chain_payload():
    def leg(ltp, bid, ask, oi, poi, iv=12.5):
        return {
            "greeks": {"delta": 0.5, "theta": -10.0, "gamma": 0.001, "vega": 12.0},
            "implied_volatility": iv,
            "last_price": ltp,
            "oi": oi,
            "previous_oi": poi,
            "volume": 1000,
            "top_bid_price": bid,
            "top_bid_quantity": 75,
            "top_ask_price": ask,
            "top_ask_quantity": 150,
        }

    return {
        "data": {
            "last_price": 24012.4,
            "oc": {
                "24050.000000": {"ce": leg(80, 79.5, 80.5, 5000, 4000), "pe": leg(110, 0, 111, 3000, 3500, iv=0)},
                "24000.000000": {"ce": leg(105, 104, 106, 9000, 8000), "pe": leg(90, 89, 91, 8000, 7000)},
            },
        },
        "status": "success",
    }


def days(start: date, n: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]
