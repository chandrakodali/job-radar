import requests, time, csv, random, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from radar.http import USER_AGENT
rows=[r["board"] for f in ("companies.csv","discovered.csv") for r in csv.DictReader(open(f)) if r["ats"]=="workday"]
random.seed(7); random.shuffle(rows)
s=requests.Session(); s.headers.update({"User-Agent":USER_AGENT,"Accept":"application/json","Content-Type":"application/json"})
i=0
def hit():
    global i
    b=rows[i%len(rows)]; i+=1
    p=b.split("|"); t,wd,site=p[0],p[1],p[2] if len(p)>2 else p[-1]
    try:
        r=s.post(f"https://{t}.{wd}.myworkdayjobs.com/wday/cxs/{t}/{site}/jobs",json={"appliedFacets":{},"limit":20,"offset":0,"searchText":""},timeout=20)
        return r.status_code, r.headers.get("Retry-After")
    except Exception as e: return "ERR", None
for rate in (1.0, 2.0, 3.0, 5.0):
    codes={}; ra=set(); t0=time.time()
    for k in range(int(60*rate) if rate<=2 else 120):
        c,r=hit(); codes[c]=codes.get(c,0)+1
        if r: ra.add(r)
        time.sleep(max(0,1/rate-0.05))
    print(f"RATE {rate}/s: {codes} retry-after={ra} in {time.time()-t0:.0f}s"); time.sleep(30)
