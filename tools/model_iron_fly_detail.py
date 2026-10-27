#!/usr/bin/env python3
"""Detail runs of the PROVISIONAL iron-fly model (tools/model_iron_fly.py): by weekday, Thursday months, entry-time and
stop sensitivity. 10-lot cost assumed Rs 800 per trade. Run from the repo root. Local files only, no orders."""
import sys,collections,datetime
src=open('tools/model_iron_fly.py').read().split('for W in (1000,1100):')[0]
exec(src)
def trades(W,dayf,stop=0.25,tp=0.30,entry='09:30',exit_='15:00',slip=8):
  out=[]
  for d in days:
    if not dayf(datetime.date.fromisoformat(d).weekday()): continue
    R=D[d]; e=next((r for r in R if r['hm']>=entry),None)
    if not e: continue
    K=e['K']; cr=fly(K,W,e); pnl=None; why='END'
    for r in R:
      if r['hm']<=e['hm']: continue
      v=fly(K,W,r)
      if stop and v>=cr*(1+stop): pnl=cr-v-slip-4; why='SL'; break
      if tp and v<=cr*(1-tp): pnl=cr-v-slip; why='TP'; break
      if r['hm']>=exit_: pnl=cr-v-slip; why='TIME'; break
    if pnl is None: pnl=cr-fly(K,W,R[-1])-slip
    out.append((d,pnl,cr,why))
  return out
def show(name,o,lots=1,ch=250):
  p=[x[1] for x in o]; n=len(p)
  net=sum(p)*20*lots-ch*n
  print('%-34s n %3d win %5.1f%% pts %+6.0f avg credit %4.0f worst %+5.0f | %2d lot net Rs %+9.0f | exits %s'%(name,n,100*sum(x>0 for x in p)/n,sum(p),sum(x[2] for x in o)/n,min(p),lots,net,dict(collections.Counter(x[3] for x in o))))
nm={0:'Mon',1:'Tue',2:'Wed',3:'Thu',4:'Fri'}
print('days',days[0],days[-1])
for wd in range(5): show('Iron fly %s'%nm[wd],trades(1000,lambda w,wd=wd:w==wd))
show('Iron fly Mon/Tue/Wed/Fri',trades(1000,lambda w:w!=3))
show('Iron fly all days',trades(1000,lambda w:w<5))
th=trades(1000,lambda w:w==3)
print('--- Thursday last 6 months (from 2026-04-27)')
t6=[x for x in th if x[0]>='2026-04-27']
show('Thu 1 lot',t6); show('Thu 10 lots',t6,10,800)
show('Thu full year 10 lots',th,10,800)
m=collections.defaultdict(list)
for x in th: m[x[0][:7]].append(x[1])
print(' '.join('%s:%+.0f(%d)'%(k[2:],sum(v)*200-800*len(v),len(v)) for k,v in sorted(m.items())))
print('--- Thursday entry-time / stop sensitivity, 1 lot')
for en in ('09:20','09:30','10:00','11:00'):
  for st,tp in ((0.25,0.30),(0.40,0.30),(0.25,0.50)):
    show('Thu %s stop%d tp%d'%(en,st*100,tp*100),trades(1000,lambda w:w==3,st,tp,en))
