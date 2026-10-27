#!/usr/bin/env python3
"""Break -> 1-min close beyond -> retest within 5 min (level holds) -> resume entry; +6/-11, 10-min; index-only (delta 0.5): python3 tools/backtest_break_retest.py candles.csv [spread_pts]"""
import sys,datetime
sys.path.insert(0,'tools'); import backtest_fresh_extreme as b
days=b.load(sys.argv[1]); ds=sorted(days); half=ds[len(ds)//2]
SPREAD=float(sys.argv[2]) if len(sys.argv)>2 else 0.0
sig=[]; seen=0
for d in ds:
  bars=days[d]; idx={x['t'].strftime('%H:%M'):k for k,x in enumerate(bars)}
  for e in b.day_events(bars):
    if not e['fresh']: continue
    seen+=1
    j=idx[e['time']]; up=e['side']=='CE'; sg=1 if up else -1; trig=e['trigger']; lvl=trig-5*sg
    # 1) a 1-min close beyond the trigger (crossing bar or the next 2)
    k=next((k for k in range(j,min(len(bars),j+3)) if (bars[k]['close']-trig)*sg>0),None)
    if k is None: continue
    # 2) retest of the broken level within the next 5 minutes; level must hold (no close back through the level)
    r=None
    for q in range(k+1,min(len(bars),k+6)):
      x=bars[q]
      if (x['close']-lvl)*sg<0: break                                  # level failed
      if ((x['low']<=trig) if up else (x['high']>=trig)): r=q; break    # price came back to the trigger zone
    if r is None: continue
    # 3) entry when price moves back in the breakout direction: next 3 bars trade beyond the retest bar's extreme
    ent=None
    for q in range(r+1,min(len(bars),r+4)):
      x=bars[q]
      if (x['close']-lvl)*sg<0: break
      lvl_hit=(x['high']>bars[r]['high']) if up else (x['low']<bars[r]['low'])
      if lvl_hit:
        ent=(q,bars[r]['high'] if up else bars[r]['low']); break
    if ent is None or b.rt.in_skip_window(bars[ent[0]]['t']): continue
    sig.append((d,ent[0],up,ent[1],e['extreme']))
def sim(d,q,up,entry,slip):
  bars=days[d]; sg=1 if up else -1
  for m,k in enumerate(range(q,min(len(bars),q+11))):
    x=bars[k]
    if m==0: adv=(entry-x['close'])*sg; fav=max(0,(x['close']-entry)*sg)    # inside the entry bar: only its close is safe
    else: adv=(entry-x['low']) if up else (x['high']-entry); fav=(x['high']-entry) if up else (entry-x['low'])
    if adv>=22: return -11-slip-SPREAD
    if fav>=12: return 6-SPREAD
  return 0.5*((bars[min(len(bars)-1,q+10)]['close']-entry)*sg)-SPREAD
print('fresh breakouts %d -> break+close+retest+resume entries %d (spread cost %.1f pts)'%(seen,len(sig),SPREAD))
for name,f in (('fresh (all)',lambda s:True),('fresh + new extreme',lambda s:s[4]),('fresh, no Thursday',lambda s:datetime.date.fromisoformat(s[0]).weekday()!=3)):
  g=[s for s in sig if f(s)]
  for slip in (0,4):
    h=[[],[]]
    for s in g: h[s[0]>=half].append(sim(*s[:4],slip))
    net=[sum(p*20 for p in x)-70*len(x) for x in h]
    w=[100*sum(p>0 for p in x)/max(1,len(x)) for x in h]
    print('  %-22s slip%d | n %3d/%3d | win %4.1f%%/%4.1f%% | pts %+6.0f/%+6.0f | net 1 lot Rs %+7.0f/%+7.0f'%(name,slip,len(h[0]),len(h[1]),*w,sum(h[0]),sum(h[1]),*net))
