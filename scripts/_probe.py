import requests, json, time, re
UA="job-radar/1.0 (personal job search tool; +https://github.com/chandrakodali/job-radar)"
s=requests.Session(); s.headers["User-Agent"]=UA
info=s.get("https://index.commoncrawl.org/collinfo.json",timeout=60).json(); api=info[0]["cdx-api"]; print("API",api)
pats=["*.homerun.co","*.hire.trakstar.com","*.keka.com","*.zohorecruit.com","*.jobs.personio.de","*.jobs.personio.com","join.com/companies/*",
"*.catsone.com","*.factorialhr.com","*.trinethire.com","app.careerpuck.com/*","*.talentreef.com","apply.jobappnetwork.com/*","*.notion.site",
"*.applytojob.com","jobs.jobvite.com/*","*.icims.com","recruiting.paylocity.com/*","*.breezy.hr","*.pinpointhq.com","*.teamtailor.com",
"jobs.gem.com/*","app.dover.com/*","jobs.dayforcehcm.com/*","workforcenow.adp.com/*","*.avature.net","*.oraclecloud.com","*.eightfold.ai",
"www.workatastartup.com/*","www.remoterocketship.com/*","www.ycombinator.com/companies/*"]
for p in pats:
    for a in range(3):
        try:
            r=s.get(api,params={"url":p,"output":"json","fl":"url","showNumPages":"true","pageSize":"1"},timeout=60)
            pages=json.loads(r.text.splitlines()[0]).get("pages",0) if r.status_code==200 else r.status_code
            r=s.get(api,params={"url":p,"output":"json","fl":"url","pageSize":"1","page":"0"},timeout=60)
            urls=[json.loads(l)["url"] for l in r.text.splitlines()[:4000]] if r.status_code==200 else []
            hosts={}
            for u in urls:
                k=re.sub(r"https?://","",u).split("?")[0][:90]
                hosts.setdefault(k.split("/")[0],k)
            print("PAT",p,"pages",pages,"hosts",len(hosts),"|",list(hosts.values())[:6]); break
        except Exception as e:
            print("PAT",p,"err",e); time.sleep(5)
    time.sleep(1)
for h in ["homerun.co","hire.trakstar.com","keka.com","zohorecruit.com","jobs.personio.de","join.com","catsone.com","factorialhr.com","trinethire.com","app.careerpuck.com","www.workatastartup.com","www.remoterocketship.com","www.notion.so","wellfound.com","builtin.com","www.ycombinator.com"]:
    try:
        t=s.get(f"https://{h}/robots.txt",timeout=20); lines=[l for l in t.text.splitlines() if l.lower().startswith(("disallow","user-agent","allow"))]
        print("ROBOTS",h,t.status_code,"|"," ; ".join(lines[:14])[:700])
    except Exception as e: print("ROBOTS",h,e)
