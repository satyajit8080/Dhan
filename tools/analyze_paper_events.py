#!/usr/bin/env python3
"""Event-level analysis of PAPER trades from decoded Cloud events (facts only).
  python3 tools/analyze_paper_events.py observation/<date>/events.jsonl
"""
import json,sys,collections,statistics as st
from datetime import datetime,timedelta
ev=[json.loads(l) for l in open(sys.argv[1])]
scans=[e for e in ev if e["event"]=="scan"]
ent={e["paperId"]:e for e in ev if e["event"]=="paper_entry"}
ex=[e for e in ev if e["event"]=="paper_exit"]
# 1-min index bars from every scan (closed bars)
bars={}
for s in scans:
    for t,o,h,l,c in (s["observation"].get("oneMinuteIndex") or {}).get("lastBars",[]): bars[t]=(o,h,l,c)
def hm(ts): return ts[11:16]
def addm(t,m):
    d=datetime.strptime(t,"%H:%M")+timedelta(minutes=m); return d.strftime("%H:%M")
def scan_before(ts):
    best=None
    for s in scans:
        if s["startedIst"]<=ts: best=s
    return best
events={}
for e in ex:
    s=ent[e["paperId"]]; k=(s["ts"],e["side"],e["trigger"]); events.setdefault(k,[]).append(e)
rows=[]
for (ts,side,trig),v in sorted(events.items()):
    t=hm(ts); sc=scan_before(ts); ob=sc["observation"]
    bar=bars.get(t); nxt=bars.get(addm(t,1))
    up= side=="CE"
    conf = bar and ((bar[3]>trig) if up else (bar[3]<trig))          # the 1-min bar of the cross closed beyond
    hold = conf and nxt and ((nxt[3]>trig) if up else (nxt[3]<trig))
    lv=ob["levels"] or {}
    cands=lv.get("resistances" if up else "supports",[])
    ref=trig-5 if up else trig+5
    L=min(cands,key=lambda x:abs(x["price"]-ref)) if cands else None
    ff=ob.get("futuresFlow") or {}
    tpn=sum(x["outcome"]=="TAKE_PROFIT" for x in v); pnl=sum(x["pnlPts"] or 0 for x in v)
    mfe=st.mean(x["maxFavourablePts"] for x in v); mae=st.mean(x["maxAdversePts"] for x in v)
    vw=ff.get("futLtpMinusVwap"); trend_ok = vw is not None and ((vw>0) if up else (vw<0))
    rows.append(dict(t=t,side=side,trig=trig,win=tpn*2>len(v),pnl=round(pnl,2),conf=bool(conf),hold=bool(hold),
        touches=L and L["touches"],src=L and "+".join(L["sources"]),vwapAligned=trend_ok,futVsVwap=vw,
        relVol=ff.get("relVolumeVsPrev10"),mfe=round(mfe,2),mae=round(mae,2),
        dist=round(abs(ob["prices"]["indexLtp"]-trig),1), held=round(st.mean(x["heldS"] for x in v))))
for r in rows: print(r)
def summ(name,f):
    a=[r for r in rows if f(r)]; b=[r for r in rows if not f(r)]
    fmt=lambda g:"%d ev, win %d, pnl %.1f"%(len(g),sum(r["win"] for r in g),sum(r["pnl"] for r in g))
    print("%-38s YES: %-28s NO: %s"%(name,fmt(a),fmt(b)))
print()
summ("1-min bar of cross closed beyond",lambda r:r["conf"])
summ("...and next bar also beyond",lambda r:r["hold"])
summ("side aligned with futures VWAP",lambda r:r["vwapAligned"])
summ("level touches>=3",lambda r:(r["touches"] or 0)>=3)
summ("relVol>=1.5 at last scan",lambda r:(r["relVol"] or 0)>=1.5)
summ("CE",lambda r:r["side"]=="CE")
summ("before 11:30",lambda r:r["t"]<"11:30")
# losers: did they go favourable first?
L=[x for x in ex if x["outcome"]=="STOP_LOSS"]; W=[x for x in ex if x["outcome"]=="TAKE_PROFIT"]
print("SL trades: MFE>=3 before stop: %d/%d; MFE==0: %d"%(sum(x["maxFavourablePts"]>=3 for x in L),len(L),sum(x["maxFavourablePts"]<=0 for x in L)))
print("TP trades: MAE<=-6 before target: %d/%d"%(sum(x["maxAdversePts"]<=-6 for x in W),len(W)))
# moneyness effect
by=collections.defaultdict(list)
for x in ex:
    s=ent[x["paperId"]]; m=(x["strike"]-s["indexAtCross"])*(1 if x["side"]=="CE" else -1)
    by["ITM" if m<-50 else "OTM" if m>50 else "ATM"].append(x["pnlPts"] or 0)
print({k:(len(v),round(sum(v),1),round(st.mean(v),2)) for k,v in by.items()})
# alternative exits simulated on recorded paths (only shrinking within the recorded path is exact)
def sim(tp,sl):
    tot=0;n=0
    for x in ex:
        e=ent[x["paperId"]]["entryAsk"]; res=None
        for _,bid in x["path"]:
            p=bid-e
            if p>=tp: res=p;break
            if p<=-sl: res=p;break
        if res is None:
            if x["outcome"]=="TAKE_PROFIT" and tp<=6: res=x["pnlPts"]
            elif x["outcome"]=="STOP_LOSS" and sl>=11: res=None
            else: res=x["pnlPts"]
        if res is None: continue
        tot+=res;n+=1
    return n,round(tot,1)
for tp,sl in [(6,11),(4,11),(6,8),(5,6),(4,6),(3,5)]: print("TP%s SL%s ->"%(tp,sl), sim(tp,sl))
