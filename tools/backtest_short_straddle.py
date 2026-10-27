#!/usr/bin/env python3
"""Short ATM straddle research on expired SENSEX weekly options (rolling ATM, expiry number 1), local files only, no orders.

Fixed-strike straddle value is rebuilt each minute from the rolling ATM straddle with a Bachelier fit around the
forward F = K + CE - PE (checked: 0.7% median error at strike switches). Close-to-close, 4 pts slippage, Rs 200/trade.
Edit O below to point at the decoded history folder."""
import json,glob,math,datetime,collections
O='/tmp/claude-0/-home-user-Dhan/e90b94d2-f527-5368-b3e1-b9c479b7bb48/scratchpad/hist_opt/history'
phi=lambda x:math.exp(-x*x/2)/math.sqrt(2*math.pi); Phi=lambda x:0.5*(1+math.erf(x/math.sqrt(2)))
def load(series):
  D={}
  for f in glob.glob(O+'/OPT_%d_ATM_CALL_*.json'%series):
    d=json.load(open(f))['from']; c=json.load(open(f))['payload']['ce']
    try: p=json.load(open(f.replace('CALL','PUT')))['payload']['pe']
    except: continue
    if not c['timestamp'] or not p['timestamp']: continue
    pm={t:i for i,t in enumerate(p['timestamp'])}
    rows=[]
    for i,t in enumerate(c['timestamp']):
      if t not in pm: continue
      j=pm[t]
      if c['strike'][i]!=p['strike'][j]: continue
      hm=datetime.datetime.utcfromtimestamp(t+19800).strftime('%H:%M')
      rows.append(dict(hm=hm,K=c['strike'][i],S=c['spot'][i],st=c['close'][i]+p['close'][j],ce=c['close'][i],pe=p['close'][j],coi=c['oi'][i],poi=p['oi'][j]))
    D[d]=rows
  return D
def straddle_at(K,r):
  # value at fixed strike K from the current ATM straddle (Bachelier)
  F=r['K']+r['ce']-r['pe']; x=F-r['K']; a=r['st']; 
  # sigma*sqrt(T) from ATM straddle at moneyness x: solve approx ignoring small x
  lo,hi=1e-3,max(3*a,1.0)
  for _ in range(40):
    s=(lo+hi)/2; d=x/s
    if x*(2*Phi(d)-1)+2*s*phi(d)>a: hi=s
    else: lo=s
  d=(F-K)/s; return (F-K)*(2*Phi(d)-1)+2*s*phi(d)
import warnings; warnings.filterwarnings('ignore')
D=load(1); days=sorted(D); half=days[len(days)//2]
def run(entry,exit_,stop_pct,dayf,slip=4,charges=200):
  out=[]
  for d in days:
    wd=datetime.date.fromisoformat(d).weekday()
    if not dayf(wd): continue
    R=D[d]; e=next((r for r in R if r['hm']>=entry),None)
    if not e: continue
    K=e['K']; prem=straddle_at(K,e); pnl=None
    for r in R:
      if r['hm']<=e['hm']: continue
      v=straddle_at(K,r)
      if stop_pct and v>=prem*(1+stop_pct): pnl=prem-v-slip-2; break
      if r['hm']>=exit_: pnl=prem-v-slip; break
    if pnl is None: pnl=prem-straddle_at(K,R[-1])-slip
    out.append((d,pnl,prem))
  return out
def rep(name,o):
  if not o: return
  h=[[p for d,p,_ in o if d<half],[p for d,p,_ in o if d>=half]]
  a=h[0]+h[1]; n=len(a); net=[sum(x)*20-200*len(x) for x in h]
  print('%-48s n %3d win %4.1f%% pts %+7.0f avg %+6.1f worst %+5.0f | net1lot Rs %+8.0f (H1 %+7.0f / H2 %+7.0f)'%(name,n,100*sum(p>0 for p in a)/n,sum(a),sum(a)/n,min(a),net[0]+net[1],net[0],net[1]))
names={0:'Mon',1:'Tue',2:'Wed',3:'Thu',4:'Fri'}
for wd in range(5): rep('short ATM straddle 09:20-15:15 %s only, no stop'%names[wd],run('09:20','15:15',0,lambda w,wd=wd:w==wd))
for st in (0,0.25,0.4):
  rep('short straddle Thu (expiry) 09:20-15:15 stop %d%%'%(st*100),run('09:20','15:15',st,lambda w:w==3))
  rep('short straddle Thu 10:30-15:15 stop %d%%'%(st*100),run('10:30','15:15',st,lambda w:w==3))
  rep('short straddle Wed 09:20-15:15 stop %d%%'%(st*100),run('09:20','15:15',st,lambda w:w==2))
  rep('short straddle ALL days 09:20-15:15 stop %d%%'%(st*100),run('09:20','15:15',st,lambda w:w<5))
