#!/usr/bin/env python3
"""Scan entry-time features of FRESH_EXTREME (Thursdays skipped) for win rates that hold in both halves: python3 tools/backtest_win_rate_scan.py candles.csv"""
import sys,datetime,statistics as st
sys.path.insert(0,'tools'); import backtest_fresh_extreme as b
days=b.load(sys.argv[1]); ds=sorted(days); half=ds[len(ds)//2]
P={}
for i,d in enumerate(ds):
  if i: P[d]=days[ds[i-1]]
rows=[]
for d in ds:
  if datetime.date.fromisoformat(d).weekday()==3: continue
  bars=days[d]; idx={x['t'].strftime('%H:%M'):k for k,x in enumerate(bars)}; n=0
  pv=P.get(d)
  for e in b.day_events(bars):
    if not(e['fresh'] and e['extreme']): continue
    j=idx[e['time']]; up=e['side']=='CE'; sg=1 if up else -1; t=e['trigger']; pre=bars[:j]
    o=bars[0]['open']; rng=max(x['high'] for x in pre)-min(x['low'] for x in pre)
    f=dict(day=d,win=e['pts']>0,pts=e['pts'],side=e['side'],hour=int(e['time'][:2]),
      mom3=(bars[j-1]['close']-bars[max(0,j-4)]['close'])*sg, mom15=(bars[j-1]['close']-bars[max(0,j-16)]['close'])*sg,
      mom30=(bars[j-1]['close']-bars[max(0,j-31)]['close'])*sg, rng=rng, vsopen=(t-o)*sg, nprior=n,
      gap=(o-pv[-1]['close'])*sg if pv else 0,
      prevtrend=(pv[-1]['close']-pv[0]['open'])*sg if pv else 0,
      beyondprev=((t>max(x['high'] for x in pv)) if up else (t<min(x['low'] for x in pv))) if pv else False,
      dprev=((t-max(x['high'] for x in pv)) if up else (min(x['low'] for x in pv)-t)) if pv else 0,
      body=(bars[j-1]['close']-bars[j-1]['open'])*sg,
      orw=max(x['high'] for x in bars[:15])-min(x['low'] for x in bars[:15]),
      wd=datetime.date.fromisoformat(d).weekday())
    rows.append(f); n+=1
H1=[r for r in rows if r['day']<half]; H2=[r for r in rows if r['day']>=half]
wr=lambda g:(100*sum(r['win'] for r in g)/len(g)) if g else 0
print('base (no Thu): H1 %.1f%% n%d  H2 %.1f%% n%d'%(wr(H1),len(H1),wr(H2),len(H2)))
feats=['mom3','mom15','mom30','rng','vsopen','nprior','gap','prevtrend','dprev','body','orw','hour']
res=[]
for k in feats:
  qs=sorted(r[k] for r in H1); cuts=[qs[int(len(qs)*p)] for p in (0.2,0.4,0.6,0.8)]
  for c in cuts:
    for op,f in (('>',lambda r,c=c:r[k]>c),('<=',lambda r,c=c:r[k]<=c)):
      a=[r for r in H1 if f(r)]; bb=[r for r in H2 if f(r)]
      if len(a)>=60 and len(bb)>=60: res.append((min(wr(a),wr(bb)),k,op,round(c,1),wr(a),len(a),wr(bb),len(bb)))
for name,f in (('beyond prev-day high/low',lambda r:r['beyondprev']),('NOT beyond prev day',lambda r:not r['beyondprev']),('CE',lambda r:r['side']=='CE'),('PE',lambda r:r['side']=='PE')):
  a=[r for r in H1 if f(r)]; bb=[r for r in H2 if f(r)]; res.append((min(wr(a),wr(bb)),name,'','',wr(a),len(a),wr(bb),len(bb)))
for wd in range(5):
  a=[r for r in H1 if r['wd']==wd]; bb=[r for r in H2 if r['wd']==wd]
  if a and bb: res.append((min(wr(a),wr(bb)),'weekday',wd,'',wr(a),len(a),wr(bb),len(bb)))
for x in sorted(res,reverse=True)[:15]: print('%-26s %-3s %-8s H1 %5.1f%% n%3d | H2 %5.1f%% n%3d'%x[1:])
