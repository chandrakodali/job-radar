import requests, time, json, csv, random, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from radar.http import USER_AGENT
rows=[r for f in ("companies.csv","discovered.csv") for r in csv.DictReader(open(f)) if r["ats"]=="workday"]
random.seed(1); sample=random.sample(rows, 12)
print("UA:", USER_AGENT)
def try_one(board, ua, label):
    parts=board.split("|"); tenant, wd, site = parts[0], parts[1], parts[2] if len(parts)>2 else parts[-1]
    url=f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    r=requests.post(url, json={"appliedFacets":{},"limit":20,"offset":0,"searchText":""}, headers={"User-Agent":ua,"Accept":"application/json","Content-Type":"application/json"}, timeout=30)
    print(label, board[:45], r.status_code, {k:v for k,v in r.headers.items() if k.lower() in ("retry-after","server","x-cache","akamai-grn","x-ratelimit-remaining","cf-ray")}, r.text[:120].replace("\n"," "))
    return r.status_code
for r in sample[:6]:
    try_one(r["board"], USER_AGENT, "OURUA"); time.sleep(2)
for r in sample[6:12]:
    try_one(r["board"], "Mozilla/5.0 (compatible; job-radar personal job search)", "ALTUA"); time.sleep(2)
print("ROBOTS", requests.get("https://"+sample[0]["board"].split("|")[0]+"."+sample[0]["board"].split("|")[1]+".myworkdayjobs.com/robots.txt", headers={"User-Agent":USER_AGENT}, timeout=20).text[:600].replace("\n"," | "))
