#!/usr/bin/env python3
"""Run with: pip install option-chain-analytics pandas; DHAN_CLIENT_ID + DHAN_TOKEN_FILE in the environment (no orders).
Live SENSEX chain (read-only Dhan) -> option-chain-analytics OptionsDataDFs; ATM vol, 25-delta skew, OI, quote diagnostics."""
import sys, time, math
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import pandas as pd, numpy as np
import option_chain_analytics as o
from sensex.dhan_client import DhanClient
from sensex.localio import credentials_from_environment
from sensex.pricing import b76_iv, b76_greeks, ist_to_epoch_ms, year_fraction_to_expiry

c = DhanClient(credentials_from_environment())
ex = c.expiry_list().payload; ex = sorted(ex.get("data", ex) if isinstance(ex, dict) else ex)[:3]
now = pd.Timestamp.now(tz="UTC").floor("s"); now_ms = int(now.timestamp() * 1000)
rows = []; spot = None
for e in ex:
    p = c.option_chain(e).payload; d = p.get("data", p)
    two = lambda sd: sum(1 for v in d["oc"].values() if (v.get(sd) or {}).get("top_bid_price") and (v.get(sd) or {}).get("top_ask_price"))
    if min(two("ce"), two("pe")) < 10:
        print("skip %s: fewer than 10 two-sided quotes per side (not trading yet)" % e); continue
    spot = d["last_price"]; oc = d["oc"]
    T = year_fraction_to_expiry(now_ms, e)
    mid = lambda l: (l["top_bid_price"] + l["top_ask_price"]) / 2 if l.get("top_bid_price") and l.get("top_ask_price") else l.get("last_price")
    ks = sorted(oc, key=float)
    atm = min((k for k in ks if oc[k].get("ce") and oc[k].get("pe")), key=lambda k: abs(mid(oc[k]["ce"]) - mid(oc[k]["pe"])))
    F = float(atm) + mid(oc[atm]["ce"]) - mid(oc[atm]["pe"])
    expiry = pd.Timestamp(e + " 15:30", tz="Asia/Kolkata").tz_convert("UTC")
    for k in ks:
        K = float(k)
        if abs(K / F - 1) > 0.06:
            continue
        for side, ot in (("ce", "C"), ("pe", "P")):
            l = oc[k].get(side)
            if not l or not mid(l):
                continue
            typ = "CE" if ot == "C" else "PE"
            iv = lambda px: (b76_iv(px, F, K, T, 0.0, typ) if px and px > 0 else None)
            m = mid(l); miv = iv(m)
            g = b76_greeks(F, K, T, miv, 0.0, typ) if miv else {}
            rows.append(dict(contract="SENSEX-%s-%s-%d" % (e, ot, K), exchange_time=now, underlying_index="SENSEX",
                forward_price=F, spot_price=spot, usd_multiplier=1.0, mark_price=m,
                bid_price=l.get("top_bid_price") or np.nan, ask_price=l.get("top_ask_price") or np.nan,
                bid_size=l.get("top_bid_quantity") or np.nan, ask_size=l.get("top_ask_quantity") or np.nan,
                mark_iv=miv or np.nan, bid_iv=iv(l.get("top_bid_price")) or np.nan, ask_iv=iv(l.get("top_ask_price")) or np.nan,
                delta=g.get("delta", np.nan), vega=g.get("vega", np.nan), theta=g.get("theta", np.nan), gamma=g.get("gamma", np.nan),
                open_interest=float(l.get("oi") or 0), volume=float(l.get("volume") or 0),
                mat_id=pd.Timestamp(e).strftime("%d%b%Y"), strike=K, optiontype=ot, expiry=expiry, ttm=T, contract_size=20.0, discount=1.0))
    time.sleep(3.1)
df = pd.DataFrame(rows)
ok = df.groupby(["mat_id", "optiontype"])["mark_iv"].apply(lambda x: x.notna().sum()).unstack().min(axis=1)
for m in ok[ok < 10].index:
    print("skip %s: fewer than 10 options with a valid implied volatility per side (not trading yet)" % m)
df = df[df.mat_id.isin(ok[ok >= 10].index)]
od = o.OptionsDataDFs(chain_ts=df, spot_data=pd.DataFrame({"close": [spot]}, index=pd.DatetimeIndex([now], name="exchange_time")), ticker="SENSEX")
chain = o.create_chain_at_time(od, now)
print("SENSEX spot %.2f at %s IST" % (spot, now.tz_convert("Asia/Kolkata").strftime("%Y-%m-%d %H:%M")))
N = o.NearestStrikeOnGrid.NEAREST
for mid_, s in chain.expiry_slices.items():
    atmv = s.get_atm_vol(N)
    jd = df[df.mat_id == mid_].set_index(["optiontype", "strike"])
    kc = s.get_call_delta_strike(0.25, N); kp = s.get_put_delta_strike(-0.25, N)
    if kc is None or kp is None:
        print(mid_, "library returned no 25-delta strike (call %s, put %s); ATM vol %.1f%%" % (kc, kp, 100 * atmv)); continue
    vc = jd.loc[("C", kc), "mark_iv"]; vp = jd.loc[("P", kp), "mark_iv"]
    oi = s.get_slice_open_interest()
    F = s.get_future_price()
    straddle = jd.loc[("C", s.get_atm_option_strike(N)), "mark_price"] + jd.loc[("P", s.get_atm_option_strike(N)), "mark_price"]
    print("%s ttm %.4fy fwd %.1f | ATM %s vol %.1f%% straddle %.0f (+/-%.2f%%) | 25d call %s vol %.1f%% | 25d put %s vol %.1f%% | "
          "25d risk reversal %.2f vol pts | 25d fly %.2f" % (mid_, s.get_ttm(), F, s.get_atm_option_strike(N), 100 * atmv, straddle,
          100 * straddle / F, kc, 100 * vc, kp, 100 * vp, 100 * (vc - vp), 100 * ((vc + vp) / 2 - atmv)))
cfg = o.QuoteDiagnosticConfig(price_unit="INR", max_relative_spread=0.05)
rep = o.diagnose_quotes(df, config=cfg, requested_time=now)
t = rep.table("expiries")
print("\nquote diagnostics (option-chain-analytics, relative spread limit 5%):")
print(t[["mat_id", "optiontype", "n_rows", "n_two_sided", "n_failed_rows", "spread_median", "relative_spread_median", "status"]].to_string())
