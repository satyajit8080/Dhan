#!/usr/bin/env python3
"""Grid of entry (calm / trend / time / opening-range) x exit (target-stop, close-back, abort) rules for FRESH_EXTREME without Thursdays, 1 strike, net of slippage and Rs70/trade: python3 tools/backtest_combined_rules.py candles.csv"""
import sys,datetime,itertools
sys.path.insert(0,'tools'); import backtest_fresh_extreme as b
days=b.load(sys.argv[1]); ds=sorted(days); half=ds[len(ds)//2]
base=[]
for d in ds:
  if datetime.date.fromisoformat(d).weekday()==3: continue
  bars=days[d]; idx={x['t'].strftime('%H:%M'):k for k,x in enumerate(bars)}
  or30=max(x['high'] for x in bars[:30])-min(x['low'] for x in bars[:30]); o=bars[0]['open']
  for e in b.day_events(bars):
    if not(e['fresh'] and e['extreme']): continue
    j=idx[e['time']]; up=e['side']=='CE'; sg=1 if up else -1
    if j<4: continue
    base.append(dict(d=d,j=j,up=up,t=e['trigger'],mom3=(bars[j-1]['close']-bars[j-4]['close'])*sg,
      trend=(bars[j-1]['close']-o)*sg>0, tm=e['time'], orpct=or30/o*100))
def sim(r,tp,sl,abort,cb,slip):
  bars=days[r['d']]; j=r['j']; up=r['up']; sg=1 if up else -1; t=r['t']
  entry=max(t,bars[j]['open']) if up else min(t,bars[j]['open']); best=0
  for m,k in enumerate(range(j,min(len(bars),j+11))):
    x=bars[k]
    if m==0: adv=-(x['close']-entry)*sg; fav=max(0,(x['close']-entry)*sg)
    else: adv=(entry-x['low']) if up else (x['high']-entry); fav=(x['high']-entry) if up else (entry-x['low'])
    if adv>=sl/0.5: return -sl-slip
    if fav>=tp/0.5: return tp
    best=max(best,fav); mv=(x['close']-entry)*sg
    if cb and (x['close']-t)*sg<0: return 0.5*mv-(1 if mv<0 else 0)*0
    if abort and m>=abort[1] and best<abort[0]: return 0.5*mv
  return 0.5*((bars[min(len(bars)-1,j+10)]['close']-entry)*sg)
entries={'calm15':lambda r:r['mom3']<=15,'calm10':lambda r:r['mom3']<=10,
 'calm12+trend':lambda r:r['mom3']<=12 and r['trend'],
 'calm15+time(<10:45|>=13:30)':lambda r:r['mom3']<=15 and (r['tm']<'10:45' or r['tm']>='13:30'),
 'calm15+OR30<0.7%':lambda r:r['mom3']<=15 and r['orpct']<0.7,
 'calm12+trend+OR<0.7%':lambda r:r['mom3']<=12 and r['trend'] and r['orpct']<0.7}
exits={'+6/-11':(6,11,None,False),'+4/-8':(4,8,None,False),'+6/-11 closeback':(6,11,None,True),
 '+6/-11 abort10in2':(6,11,(10,2),False),'+4/-8 closeback':(4,8,None,True),'+4/-8 abort8in3':(4,8,(8,3),False)}
res=[]
for (en,ef),(xn,(tp,sl,ab,cb)) in itertools.product(entries.items(),exits.items()):
  g=[r for r in base if ef(r)]
  for slip in (2,4):
    h=[[],[]]
    for r in g: h[r['d']>=half].append(sim(r,tp,sl,ab,cb,slip))
    net=[sum(p*20 for p in x)-70*len(x) for x in h]
    res.append((min(net),en,xn,slip,len(h[0]),len(h[1]),100*sum(p>0 for p in h[0]+h[1])/max(1,len(g)),sum(h[0]),sum(h[1]),net[0],net[1]))
for x in sorted(res,reverse=True)[:12]:
  print('%-28s %-18s slip%d n%3d/%3d win %4.1f%% pts %+5.0f/%+5.0f net Rs %+6.0f/%+6.0f'%x[1:])
print('positive after costs in both halves:',sum(1 for x in res if x[0]>0),'of',len(res))
