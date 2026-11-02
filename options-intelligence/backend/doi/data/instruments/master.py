"""Dhan instrument master: parse, version and resolve indices and contracts.

Nothing here hard-codes security ids, lot sizes, strike steps or expiry weekdays;
they all come from the master file, which is versioned by its SHA-256."""

from __future__ import annotations

import csv
import hashlib
import io
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import pairwise

from doi.core.errors import InstrumentError
from doi.core.models import ExpiryProfile, Instrument, UnderlyingRef

_ALIASES: dict[str, tuple[str, ...]] = {
    "exchange": ("EXCH_ID", "SEM_EXM_EXCH_ID"),
    "segment": ("SEGMENT", "SEM_SEGMENT"),
    "security_id": ("SECURITY_ID", "SEM_SMST_SECURITY_ID"),
    "instrument": ("INSTRUMENT", "SEM_INSTRUMENT_NAME", "INSTRUMENT_TYPE"),
    "underlying_security_id": ("UNDERLYING_SECURITY_ID",),
    "underlying_symbol": ("UNDERLYING_SYMBOL", "SM_SYMBOL_NAME"),
    "symbol": ("DISPLAY_NAME", "SEM_TRADING_SYMBOL", "SYMBOL_NAME", "SEM_CUSTOM_SYMBOL"),
    "expiry": ("SM_EXPIRY_DATE", "SEM_EXPIRY_DATE"),
    "strike": ("STRIKE_PRICE", "SEM_STRIKE_PRICE"),
    "option_type": ("OPTION_TYPE", "SEM_OPTION_TYPE"),
    "lot_size": ("LOT_SIZE", "SEM_LOT_UNITS"),
    "tick_size": ("TICK_SIZE", "SEM_TICK_SIZE"),
}
_REQUIRED = (
    "exchange",
    "segment",
    "security_id",
    "instrument",
    "underlying_symbol",
    "expiry",
    "strike",
    "option_type",
    "lot_size",
)
_KNOWN_INSTRUMENTS = {"OPTIDX", "FUTIDX", "INDEX", "OPTSTK", "FUTSTK", "EQUITY", "OPTFUT", "FUTCOM", "OPTCUR", "FUTCUR"}
_SEGMENTS = {("NSE", "D"): "NSE_FNO", ("BSE", "D"): "BSE_FNO", ("NSE", "I"): "IDX_I", ("BSE", "I"): "IDX_I"}


def _num(v: str | None) -> float | None:
    try:
        x = float(v) if v not in (None, "") else None
    except ValueError:
        return None
    return x if x is not None and math.isfinite(x) else None


def _date(v: str | None) -> date | None:
    if not v or v.strip() in ("", "-1", "0"):
        return None
    s = v.strip()[:10]
    for parse in (date.fromisoformat, lambda t: date(int(t[6:10]), int(t[3:5]), int(t[0:2]))):
        try:
            return parse(s)
        except ValueError:
            continue
    return None


@dataclass(frozen=True, slots=True)
class MasterVersion:
    version: str  # sha256 of the raw bytes
    retrieved_at: datetime
    rows: int


class InstrumentMaster:
    def __init__(self, instruments: list[Instrument], version: MasterVersion) -> None:
        self.version = version
        self._options: dict[tuple[str, str], list[Instrument]] = defaultdict(list)
        self._indices: dict[tuple[str, str], list[Instrument]] = defaultdict(list)
        for ins in instruments:
            if ins.instrument == "OPTIDX" and ins.underlying_symbol:
                self._options[(ins.exchange, ins.underlying_symbol.upper())].append(ins)
            elif ins.instrument == "INDEX" and ins.exchange_segment == "IDX_I" and ins.underlying_symbol:
                self._indices[(ins.exchange, ins.underlying_symbol.upper())].append(ins)

    @classmethod
    def parse(cls, raw: bytes | str, retrieved_at: datetime | None = None) -> InstrumentMaster:
        data = raw.encode() if isinstance(raw, str) else raw
        text = data.decode("utf-8-sig")
        reader = csv.reader(io.StringIO(text))
        try:
            header = [h.strip().upper() for h in next(reader)]
        except StopIteration:
            raise InstrumentError("instrument master is empty") from None
        idx: dict[str, list[int]] = {}
        for field, names in _ALIASES.items():
            idx[field] = [header.index(n) for n in names if n in header]
        missing = [f for f in _REQUIRED if not idx[f]]
        if missing:
            raise InstrumentError(f"instrument master missing columns for {missing}; header starts {header[:12]}")

        def get(row: list[str], field: str) -> str | None:
            vals = [row[i].strip() for i in idx[field] if i < len(row) and row[i].strip()]
            if field == "instrument":
                return next((v for v in vals if v.upper() in _KNOWN_INSTRUMENTS), vals[0] if vals else None)
            return vals[0] if vals else None

        out: list[Instrument] = []
        for row in reader:
            if not row:
                continue
            exch, seg, sid = (get(row, "exchange") or "").upper(), (get(row, "segment") or "").upper(), get(row, "security_id")
            if not sid:
                continue
            opt = (get(row, "option_type") or "").upper()
            lot = _num(get(row, "lot_size"))
            out.append(
                Instrument(
                    security_id=sid,
                    symbol=get(row, "symbol") or sid,
                    exchange=exch,
                    exchange_segment=_SEGMENTS.get((exch, seg), f"{exch}_{seg}"),
                    instrument=(get(row, "instrument") or "").upper(),
                    underlying_symbol=get(row, "underlying_symbol"),
                    underlying_security_id=get(row, "underlying_security_id"),
                    expiry=_date(get(row, "expiry")),
                    strike=_num(get(row, "strike")),
                    option_type=opt if opt in ("CE", "PE") else None,  # type: ignore[arg-type]
                    lot_size=int(lot) if lot else None,
                    tick_size=_num(get(row, "tick_size")),
                )
            )
        version = MasterVersion(hashlib.sha256(data).hexdigest(), retrieved_at or datetime.now(UTC), len(out))
        return cls(out, version)

    def options(self, exchange: str, symbol: str) -> list[Instrument]:
        return self._options.get((exchange.upper(), symbol.upper()), [])

    def resolve_underlying(self, key: str, exchange_family: str, master_symbol: str, today: date | None = None) -> UnderlyingRef:
        opts = [o for o in self.options(exchange_family, master_symbol) if o.expiry and (today is None or o.expiry >= today)]
        if not opts:
            raise InstrumentError(f"{key}: no active OPTIDX contracts for {exchange_family}:{master_symbol}")
        # The option-chain/quote APIs take the INDEX row's security id (IDX_I), e.g. NIFTY 13.
        # The options' UNDERLYING_SECURITY_ID is a different code (NIFTY 26000 in the live master,
        # verified 2 Nov 2026), so it is used only for the consistency check below.
        idx = self._indices.get((exchange_family.upper(), master_symbol.upper()), [])
        if len(idx) != 1:
            raise InstrumentError(
                f"{key}: {'no' if not idx else 'ambiguous'} IDX_I INDEX row for {exchange_family}:{master_symbol}"
            )
        index_id = idx[0].security_id
        uids = {o.underlying_security_id for o in opts if o.underlying_security_id}
        if len(uids) != 1:
            raise InstrumentError(
                f"{key}: underlying security id is {'missing' if not uids else 'ambiguous ' + str(sorted(uids))}"
            )
        segs = {o.exchange_segment for o in opts}
        if len(segs) != 1:
            raise InstrumentError(f"{key}: derivative segment ambiguous {sorted(segs)}")

        by_exp: dict[date, list[Instrument]] = defaultdict(list)
        for o in opts:
            by_exp[o.expiry].append(o)  # type: ignore[index]
        profiles = []
        for exp in sorted(by_exp):
            contracts = by_exp[exp]
            lots = {c.lot_size for c in contracts if c.lot_size}
            if len(lots) != 1:
                raise InstrumentError(f"{key} {exp}: lot size {'missing' if not lots else 'inconsistent ' + str(sorted(lots))}")
            strikes = sorted({c.strike for c in contracts if c.strike})
            diffs = Counter(round(b - a, 4) for a, b in pairwise(strikes) if b > a)
            if not diffs:
                raise InstrumentError(f"{key} {exp}: cannot infer strike step from {len(strikes)} strike(s)")
            step = diffs.most_common(1)[0][0]
            profiles.append(ExpiryProfile(exp, lots.pop(), step, len(contracts)))
        return UnderlyingRef(
            key=key,
            exchange_family=exchange_family.upper(),  # type: ignore[arg-type]
            security_id=index_id,
            segment="IDX_I",
            derivative_segment=segs.pop(),
            master_version=self.version.version,
            expiries=tuple(profiles),
        )

    def contract(self, exchange_family: str, master_symbol: str, expiry: date, strike: float, option_type: str) -> Instrument:
        hits = [
            o
            for o in self.options(exchange_family, master_symbol)
            if o.expiry == expiry and o.option_type == option_type and o.strike is not None and abs(o.strike - strike) < 1e-6
        ]
        if len(hits) != 1:
            raise InstrumentError(
                f"{master_symbol} {expiry} {strike} {option_type}: {'no contract' if not hits else f'{len(hits)} ambiguous contracts'}"
            )
        return hits[0]
