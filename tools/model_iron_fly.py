#!/usr/bin/env python3
"""PROVISIONAL iron-fly model on expired SENSEX weekly options (rolling ATM, expiry number 1), local files only, no orders.

Short ATM straddle from real premiums (Bachelier fit around F = K + CE - PE, as in backtest_short_straddle.py). The two
wings (ATM +- W) are MODEL prices: flat vol from the ATM straddle, no skew, until the history_wings download arrives.
Entry 09:30, exit 15:00, stop at credit +25%, take profit at 30% of credit; Rs 250 + 8 pts slippage per 4-leg trade, lot 20.
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
def sig(r):
  F=r['K']+r['ce']-r['pe']; x=F-r['K']; a=r['st']; lo,hi=1e-3,max(3*a,1.0)
  for _ in range(40):
    s=(lo+hi)/2; d=x/s
    if x*(2*Phi(d)-1)+2*s*phi(d)>a: hi=s
    else: lo=s
  return F,s
def fly(K,W,r):
  F,s=sig(r); st=straddle_at(K,r)
  dc=(F-K-W)/s; c=(F-K-W)*Phi(dc)+s*phi(dc)
  dp=(K-W-F)/s; p=(K-W-F)*Phi(dp)+s*phi(dp)
  return st-c-p
D=load(1); days=sorted(D); half=days[len(days)//2]
def run(W,dayf,stop,tp,entry='09:30',exit_='15:00',slip=8,ch=250):
  out=[]
  for d in days:
    if not dayf(datetime.date.fromisoformat(d).weekday()): continue
    R=D[d]; e=next((r for r in R if r['hm']>=entry),None)
    if not e: continue
    K=e['K']; cr=fly(K,W,e); pnl=None
    for r in R:
      if r['hm']<=e['hm']: continue
      v=fly(K,W,r)
      if stop and v>=cr*(1+stop): pnl=cr-v-slip-4; break
      if tp and v<=cr*(1-tp): pnl=cr-v-slip; break
      if r['hm']>=exit_: pnl=cr-v-slip; break
    if pnl is None: pnl=cr-fly(K,W,R[-1])-slip
    out.append((d,pnl,cr))
  h=[[p for d,p,_ in out if d<half],[p for d,p,_ in out if d>=half]]; a=h[0]+h[1]
  net=[sum(x)*20-ch*len(x) for x in h]
  return len(a),100*sum(p>0 for p in a)/len(a),sum(a),min(a),sum(c for *_,c in out)/len(out),net
for W in (1000,1100):
 for nm,f in (('Mon/Tue/Wed/Fri',lambda w:w in(0,1,2,4)),('Thu only',lambda w:w==3),('All days',lambda w:w<5)):
  for stop,tp in ((0.25,0.30),(0.25,0),(0,0.30),(0,0)):
    n,win,pts,worst,cr,net=run(W,f,stop,tp)
    print('W%d %-16s stop%3d%% tp%3d%% | n %3d win %4.1f%% pts %+6.0f worst %+5.0f credit %5.0f | net Rs %+7.0f (H1 %+7.0f H2 %+7.0f)'%(W,nm,stop*100,tp*100,n,win,pts,worst,cr,net[0]+net[1],net[0],net[1]))
