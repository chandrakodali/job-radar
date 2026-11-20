import sys, os, time, json, csv, random, requests
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from radar import ats as A
from radar.models import Board
from radar.http import session, USER_AGENT
import radar.http as H
rows=[r for f in ("companies.csv","discovered.csv") for r in csv.DictReader(open(f)) if r["ats"]=="workday"]
st=json.load(open("data/ot/state.json")); ra=st.get("read_at",{})
rows.sort(key=lambda r: ra.get("workday:"+r["board"],""))
print("first boards by radar order:", [r["board"] for r in rows[:5]])
base,_=A._wd_hosts(rows[0]["board"]); print("BASE", base)
# raw request with the radar's own session, no retries, show body on non-200
s=requests.Session(); s.headers.update(session().headers)
print("SESSION HEADERS", dict(session().headers))
for r in rows[:6]:
    base,_=A._wd_hosts(r["board"])
    resp=s.post(base+"/jobs", json={"appliedFacets":{},"limit":20,"offset":0,"searchText":""}, timeout=20)
    print("RAW", r["board"][:40], resp.status_code, resp.headers.get("server"), resp.headers.get("retry-after"), resp.text[:200].replace("\n"," "))
    time.sleep(1.5)
for r in rows[6:10]:
    b=Board(ats="workday", board=r["board"], company=r["company"])
    t=time.time()
    try:
        jobs=A.workday_list(b, max_pages=15, us_only=True); print("RADAR", r["board"][:40], "jobs", len(jobs), round(time.time()-t,1),"s")
    except Exception as e: print("RADAR", r["board"][:40], "ERR", type(e).__name__, e, getattr(e,"status",None))
    time.sleep(1.5)
