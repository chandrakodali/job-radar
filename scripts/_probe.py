import sys, os, time, json, csv
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from radar.profile import activate
activate("network")
from radar import ats as A, http as H, store as S
from radar.models import Board
import logging
st=json.load(open("data/network/state.json")); ra=st.get("read_at",{})
boards=[b for b in S.load_boards() if b.ats=="workday"]
boards.sort(key=lambda b: ra.get(b.key,""))
print("first", [b.board for b in boards[:8]])
log=[]
sess=H.session()
sess.hooks["response"].append(lambda r,*a,**k: log.append((round(time.time()%1000,1), r.status_code, r.request.method, r.url[:90], r.headers.get("retry-after"), r.text[:80].replace("\n"," ") if r.status_code!=200 else "")))
H.set_pace("myworkdayjobs.com", 0.9)
t0=time.time()
for b in boards[:12]:
    n=len(log)
    try:
        jobs=A.workday_list(b, max_pages=15, us_only=True); res=f"jobs {len(jobs)}"
    except Exception as e: res=f"ERR {type(e).__name__} {e}"
    print("BOARD", b.board[:45], res, round(time.time()-t0,1))
    for x in log[n:]: print("   HTTP", x)
