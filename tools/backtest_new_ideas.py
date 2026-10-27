#!/usr/bin/env python3
"""Opening-range breakout and failed-FRESH_EXTREME fade on 1-min SENSEX bars (index-only, delta 0.5): python3 tools/backtest_new_ideas.py candles.csv"""
import sys
from datetime import time
sys.path.insert(0,'tools'); import backtest_fresh_extreme as b
days=b.load(sys.argv[1]); ds=sorted(days); half=ds[len(ds)//2]
D=0.5
def run(bars,j,entry,up,tp,sl,slip,hold):
  sg=1 if up else -1
  for k in range(j+1,min(len(bars),j+1+hold)):
    x=bars[k]; adv=(entry-x['low']) if up else (x['high']-entry); fav=(x['high']-entry) if up else (entry-x['low'])
    if adv>=sl/D: return -sl-slip
    if fav>=tp/D: return tp
  k=min(len(bars)-1,j+hold); return D*(bars[k]['close']-entry)*sg
def orb(bars):
  r=[x for x in bars if time(9,16)<=x['t'].time()<=time(9,30)]
  hi=max(x['high'] for x in r); lo=min(x['low'] for x in r)
  for j,x in enumerate(bars):
    if x['t'].time()<=time(9,30): continue
    if x['t'].time()>=time(11,30): return None
    if x['close']>hi: return (j,x['close'],True)
    if x['close']<lo: return (j,x['close'],False)
def fades(bars):
  out=[]; idx={x['t'].strftime('%H:%M'):k for k,x in enumerate(bars)}
  for e in b.day_events(bars):
    if not(e['fresh'] and e['extreme']): continue
    j=idx[e['time']]; up=e['side']=='CE'; t=e['trigger']
    for k in range(j,min(len(bars),j+4)):
      c=bars[k]['close']
      if (c<t) if up else (c>t):
        if not b.rt.in_skip_window(bars[k]['t']): out.append((k,c,not up))
        break
  return out
sigs={'A ORB15':{d:([orb(days[d])] if orb(days[d]) else []) for d in ds},'B fade failed FX':{d:fades(days[d]) for d in ds}}
for name,S_ in sigs.items():
  n=sum(len(v) for v in S_.values())
  print(name,'signals',n)
  for tp,sl,hold in ((6,11,10),(10,10,20),(15,10,30),(20,10,30)):
    for slip in (0,4):
      h=[0.0,0.0]; w=0
      for d,v in S_.items():
        for (j,entry,up) in v:
          p=run(days[d],j,entry,up,tp,sl,slip,hold); h[d>=half]+=p; w+=p>0
      print('   TP +%-2d SL -%-2d hold %2dm slip %d: win %.1f%%  H1 %+7.0f  H2 %+7.0f'%(tp,sl,hold,slip,100*w/max(n,1),h[0],h[1]))
