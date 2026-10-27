#!/usr/bin/env python3
"""Early-exit what-ifs for FRESH_EXTREME on 1-min SENSEX bars: python3 tools/backtest_fresh_extreme_exits.py candles.csv"""
import sys
sys.path.insert(0,'tools'); import backtest_fresh_extreme as b
days=b.load(sys.argv[1]); ds=sorted(days); half=ds[len(ds)//2]
ev=[]
for d in ds:
  bars=days[d]; idx={x['t'].strftime('%H:%M'):k for k,x in enumerate(bars)}
  for e in b.day_events(bars):
    if e['fresh'] and e['extreme']: ev.append((d,idx[e['time']],e['side']=='CE',e['trigger']))
def sim(d,j,up,trig,rule,slip):
  bars=days[d]; sg=1 if up else -1; entry=max(trig,bars[j]['open']) if up else min(trig,bars[j]['open'])
  best=0
  for m,k in enumerate(range(j,min(len(bars),j+11))):
    x=bars[k]
    if m==0: adv=-(x['close']-entry)*sg; fav=max(0,(x['close']-entry)*sg)
    else: adv=(entry-x['low'])*sg if up else (x['high']-entry); fav=(x['high']-entry) if up else (entry-x['low'])
    if adv>=22: return -11-slip
    if fav>=12: return 6
    best=max(best,fav); close_mv=(x['close']-entry)*sg
    kind,a,n=rule
    if kind=='noprog' and m>=n and best<a: return 0.5*close_mv-(slip if close_mv<0 else 0)*0
    if kind=='closeback' and m>=0 and close_mv< -a: return 0.5*close_mv
  return 0.5*((bars[min(len(bars)-1,j+10)]['close']-entry)*sg)
rules=[('none',0,0),('closeback',0,0),('closeback',5,0),('noprog',12,2),('noprog',12,3),('noprog',8,2),('noprog',6,1),('noprog',12,5)]
for slip in (0,4):
  print('stop slippage',slip,'(stop = -%d)'%(11+slip))
  for r in rules:
    h1=sum(sim(*e,r,slip) for e in ev if e[0]<half); h2=sum(sim(*e,r,slip) for e in ev if e[0]>=half)
    h1t=sum(sim(*e,r,slip) for e in ev if e[0]<half and __import__('datetime').date.fromisoformat(e[0]).weekday()!=3)
    h2t=sum(sim(*e,r,slip) for e in ev if e[0]>=half and __import__('datetime').date.fromisoformat(e[0]).weekday()!=3)
    print('  %-22s H1 %+7.0f  H2 %+7.0f  total %+7.0f | skip Thu: H1 %+7.0f H2 %+7.0f'%(r,h1,h2,h1+h2,h1t,h2t))
