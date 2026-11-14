import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from radar import ats as A
from radar.models import Board
from radar.store import load_companies
from radar.match import classify_location
for ats, board in [("amazon","us"),("apple","usa"),("ibm","us"),("tiktok","us")]:
    t=time.time()
    try:
        jobs=A.LISTERS[ats](Board(ats=ats,board=board,company=""))
        us=sum(1 for j in jobs if classify_location(j.location,j.countries)!="non_us")
        print("BIG",ats,"jobs",len(jobs),"us",us,"secs",round(time.time()-t),"| sample:",[(j.title,j.location,str(j.posted_at)[:10],j.company,len(j.description or "")) for j in jobs[:3]])
        if ats=="ibm" and jobs:
            A.DETAILERS["ibm"](jobs[0]); print("   ibm detail:",jobs[0].posted_at,len(jobs[0].description or ""))
    except Exception as e: print("BIG",ats,"ERR",type(e).__name__,str(e)[:300])
try:
    c=[b for b in load_companies() if b.ats in ("amazon","apple","ibm","tiktok")]; print("LOADED",[(b.ats,b.board) for b in c])
except Exception as e: print("LOAD ERR",e)
