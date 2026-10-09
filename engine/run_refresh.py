from datetime import datetime
from refresh_table import build_refresh_table, Leg, Level, to_markdown, to_compact, black76

R = [Level(74723.5,12,["swing_low"]), Level(74754.65,12,["swing_high","opening_range_high"]),
     Level(74780.66,6,["swing_high","session_high","consolidation_high","previous_session_high"])]
S = [Level(74657.3357,11,["vwap","swing_high"]), Level(74595.425,12,["swing_low","session_low","opening_range_low","consolidation_low","swing_high"]),
     Level(74567.35,10,["swing_high"]), Level(74508.68,15,["swing_low"]), Level(74465.15,1,["swing_low","previous_session_low"])]
L = [Leg(74500,True,12.9454,445,445.5,445.7), Leg(74500,False,12.9602,286.7,287.35,287.3),
     Leg(74600,True,12.8435,387.75,388.35,388.35), Leg(74600,False,12.8529,329.35,330,329.75),
     Leg(74700,True,12.7279,335,335.05,335), Leg(74700,False,12.7519,376.3,376.9,376.75),
     Leg(74800,True,12.6343,286.35,286.75,286.75), Leg(74800,False,12.6606,428,428.55,428.5)]
F, T, DF = 74658.8947, 0.008782546581684423, 0.999429297385

for l in L:
    m = black76(F, l.strike, T, l.iv_pct/100, DF, l.is_call)
    assert abs(m - l.ltp) < 1.5, (l.strike, l.is_call, round(m,2), l.ltp)
print("model reprices LTPs: OK")

rows = build_refresh_table(forward=F, T=T, df=DF, candle_ref_price=74667.55,
    resistances=R, supports=S, atr=36.91, bar_minutes=5, legs=L,
    snapshot_ms=1789967033611, candles_ms=1789966980000,
    now=datetime(2026,9,21,10,33))
print(to_compact(rows))
