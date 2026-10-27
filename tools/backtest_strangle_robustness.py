#!/usr/bin/env python3
"""Robustness of the short strangle held to expiry (paper research, local files only, no orders).
Strike distance, delta-based (adaptive) strikes, entry time, entry day, simple walk-forward.
Strikes beyond +-1,000 use the linear-extrapolated smile (no data there).
Usage: python3 tools/backtest_strangle_robustness.py PICKLE  (PICKLE = pickle.dump(backtest_wings.load(...)))"""
import sys,pickle,statistics as st,collections; sys.path.insert(0,'tools'); import backtest_wings as w
D=pickle.load(open(sys.argv[1],'rb')); w.EXP.update(w.expiries(D)); w.EXTRAP['mode']='linear'
def dist(n):  # sell call K+n, put K-n (100-pt strikes)
  return lambda r:[('C',r['K']+n,1),('P',r['K']-n,1)]
def delta(t):
  def f(r):
    out=[]
    for kind in 'CP':
      best=None
      for k in range(1,25):
        K=r['K']+100*k if kind=='C' else r['K']-100*k
        s=w.sig(r,K); d=w.Phi((r['F']-K)/s) if kind=='C' else w.Phi((K-r['F'])/s)
        if d<=t: best=K; break
      out.append((kind,best or (r['K']+2400 if kind=='C' else r['K']-2400),1))
    return out
  return f
def go(name,legs,entry='09:30',ef=w.prev_day(2)):
  w.STRUCT['X']=(legs,150.0,4.0); o=w.run(D,'X',ef,entry=entry)
  n=[x['net'] for x in o]; half=o[len(o)//2]['day']
  h1=sum(x['net'] for x in o if x['day']<half); h2=sum(n)-h1
  eq=pk=dd=0
  for v in n: eq+=v; pk=max(pk,eq); dd=min(dd,eq-pk)
  print('%-30s n %d win %5.1f%% net %+8.0f (H1 %+7.0f / H2 %+7.0f) worst %+7.0f maxDD %+7.0f t %.2f credit %.0f'%(name,len(n),100*sum(v>0 for v in n)/len(n),sum(n),h1,h2,min(n),dd,st.mean(n)/st.stdev(n)*len(n)**.5,st.mean(x['credit'] for x in o)))
  return h1,h2
print('-- strike distance (Tue 09:30, hold to expiry; beyond 1,000 = extrapolated)')
r={}
for n in (600,800,1000,1200,1500): r[n]=go('strangle +-%d'%n,dist(n))
print('-- adaptive: strike by delta (Ajit-style)')
for t in (0.20,0.15,0.10,0.05): r['d%.2f'%t]=go('strangle delta %.2f'%t,delta(t))
print('-- entry time (+-1000)')
for e in ('09:30','11:00','13:00','15:00'): go('strangle +-1000 entry %s'%e,dist(1000),e)
print('-- entry day (+-1000)')
for k in (3,2,1): go('strangle +-1000 %d days before'%k,dist(1000),ef=w.prev_day(k))
print('-- walk-forward: pick best of the variants on H1, see its H2')
b=max(r,key=lambda k:r[k][0]); print('best on H1:',b,'H1 %+.0f -> H2 %+.0f'%r[b])
