import requests, json, time, re
UA="job-radar/1.0 (personal job search tool; +https://github.com/chandrakodali/job-radar)"
s=requests.Session(); s.headers["User-Agent"]=UA
info=s.get("https://index.commoncrawl.org/collinfo.json",timeout=60).json()
apis=[c["cdx-api"] for c in info[:3]]
def sample(p, n=8):
    for api in apis:
        for a in range(4):
            try:
                r=s.get(api,params={"url":p,"output":"json","fl":"url","pageSize":"1","page":"0","limit":"3000"},timeout=90)
                if r.status_code!=200: time.sleep(6*(a+1)); continue
                hosts={}
                for l in r.text.splitlines():
                    u=json.loads(l)["url"]; k=re.sub(r"https?://","",u).split("?")[0]
                    hosts.setdefault(k.split("/")[0]+("/"+k.split("/")[1] if "/" in k and k.split("/")[0] in ("app.careerpuck.com","jobs.jobvite.com","jobs.gem.com","app.dover.com","jobs.dayforcehcm.com","join.com") else ""), u)
                if hosts: return list(hosts.values())[:n]
            except Exception as e: time.sleep(6)
    return []
def show(url, n=1500, **kw):
    try:
        r=s.get(url,timeout=30,**kw); t=r.text
        print("GET",url,r.status_code,r.headers.get("content-type","")[:40],len(t),"|",re.sub(r"\s+"," ",t[:n]))
        return r
    except Exception as e: print("GET",url,e)
for p in ["*.homerun.co","*.factorialhr.com","app.careerpuck.com/*","*.trinethire.com","*.keka.com","*.applytojob.com","jobs.jobvite.com/*","*.icims.com","*.pinpointhq.com","*.breezy.hr","*.teamtailor.com","jobs.gem.com/*","app.dover.com/*","jobs.dayforcehcm.com/*","workforcenow.adp.com/*","*.avature.net","*.oraclecloud.com","*.eightfold.ai","recruiting.paylocity.com/*","*.notion.site"]:
    print("SAMPLE",p,sample(p)); time.sleep(1)
# new systems: page shapes
show("https://3rtalent.zohorecruit.com/jobs/Careers", 2500)
show("https://3rtalent.zohorecruit.com/robots.txt", 300)
show("https://1komma5grad.jobs.personio.de/xml", 1200)
show("https://1komma5grad.jobs.personio.de/robots.txt", 300)
show("https://3kc.catsone.com/careers/", 2500)
show("https://3kc.catsone.com/robots.txt", 300)
show("https://join.com/companies/02100", 1500)
show("https://www.ycombinator.com/jobs", 2500)
show("https://www.workatastartup.com/jobs", 1500)
show("https://www.remoterocketship.com/terms", 3000)
