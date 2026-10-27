#!/usr/bin/env python3
"""Same level/breakout logic on Nifty 1-min CSV (datetime,open,high,low,close): python3 tools/backtest_nifty.py nifty.csv TP SL SLIP"""
import sys,csv,datetime
from collections import defaultdict
sys.path.insert(0,'tools'); import backtest_fresh_extreme as b
def load(path):
  days=defaultdict(list)
  for r in csv.DictReader(open(path)):
    ts=datetime.datetime.strptime(r['datetime'][:19],'%Y-%m-%dT%H:%M:%S')
    if ts.hour==9 and ts.minute==15: continue
    days[ts.date().isoformat()].append({'t':ts,'timestampMs':int(ts.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=5,minutes=30))).timestamp()*1000),
      'open':float(r['open']),'high':float(r['high']),'low':float(r['low']),'close':float(r['close'])})
  return {d:v for d,v in days.items() if len(v)>300}
days=load(sys.argv[1]); ds=sorted(days); half=ds[len(ds)//2]
TP,SL,SLIP=[float(x) for x in sys.argv[2:5]]                # premium points (delta 0.5)
ev=[]; retest=[]
for d in ds:
  bars=days[d]; idx={x['t'].strftime('%H:%M'):k for k,x in enumerate(bars)}
  for e in b.day_events(bars):
    j=idx[e['time']]; up=e['side']=='CE'; sg=1 if up else -1; trig=e['trigger']
    ev.append(dict(d=d,j=j,up=up,trig=trig,fresh=e['fresh'],ext=e['extreme'],mom3=(bars[j-1]['close']-bars[j-4]['close'])*sg if j>=4 else 99))
    if not e['fresh']: continue
    lvl=trig-5*sg; k=next((k for k in range(j,min(len(bars),j+3)) if (bars[k]['close']-trig)*sg>0),None)
    if k is None: continue
    r=None
    for q in range(k+1,min(len(bars),k+6)):
      x=bars[q]
      if (x['close']-lvl)*sg<0: break
      if (x['low']<=trig) if up else (x['high']>=trig): r=q; break
    if r is None: continue
    for q in range(r+1,min(len(bars),r+4)):
      x=bars[q]
      if (x['close']-lvl)*sg<0: break
      if (x['high']>bars[r]['high']) if up else (x['low']<bars[r]['low']):
        if not b.rt.in_skip_window(x['t']): retest.append(dict(d=d,j=q,up=up,entry=bars[r]['high'] if up else bars[r]['low']))
        break
def sim(d,j,up,entry):
  bars=days[d]; sg=1 if up else -1
  for m,k in enumerate(range(j,min(len(bars),j+11))):
    x=bars[k]
    if m==0: adv=(entry-x['close'])*sg; fav=max(0,(x['close']-entry)*sg)
    else: adv=(entry-x['low']) if up else (x['high']-entry); fav=(x['high']-entry) if up else (entry-x['low'])
    if adv>=SL/0.5: return -SL-SLIP
    if fav>=TP/0.5: return TP
  return 0.5*((bars[min(len(bars)-1,j+10)]['close']-entry)*sg)
def entry_of(e,bars):
  return max(e['trig'],bars[e['j']]['open']) if e['up'] else min(e['trig'],bars[e['j']]['open'])
sets={'all breakouts':[e for e in ev],'fresh':[e for e in ev if e['fresh']],'fresh+extreme':[e for e in ev if e['fresh'] and e['ext']],
      'fresh+extreme+calm(<=5)':[e for e in ev if e['fresh'] and e['ext'] and e['mom3']<=5]}
print('days',len(ds),'| TP +%g SL -%g slip %g (premium pts)'%(TP,SL,SLIP))
for name,g in list(sets.items())+[('BREAK-RETEST (your rule)',None)]:
  h=[[],[]]
  if g is None:
    for e in retest: h[e['d']>=half].append(sim(e['d'],e['j'],e['up'],e['entry']))
  else:
    for e in g: h[e['d']>=half].append(sim(e['d'],e['j'],e['up'],entry_of(e,days[e['d']])))
  n=[len(x) for x in h]; w=[100*sum(p>0 for p in x)/max(1,len(x)) for x in h]; p=[sum(x) for x in h]
  print('  %-26s n %4d/%4d win %4.1f%%/%4.1f%% pts %+7.1f/%+7.1f per trade %+.2f/%+.2f'%(name,*n,*w,*p,p[0]/max(1,n[0]),p[1]/max(1,n[1])))
