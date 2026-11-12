import requests, json, re
UA="job-radar/1.0 (personal job search tool; +https://github.com/chandrakodali/job-radar)"
s=requests.Session(); s.headers["User-Agent"]=UA
def robots(host, paths):
    try:
        t=s.get(f"https://{host}/robots.txt",timeout=20).text
        dis=[l.strip() for l in t.splitlines() if l.lower().startswith("disallow")]
        print("ROBOTS",host,"| relevant:",[d for d in dis if any(p in d for p in paths)][:12], "| total disallow:",len(dis))
    except Exception as e: print("ROBOTS",host,e)
robots("www.amazon.jobs",["search","json","/en"])
robots("apply.careers.microsoft.com",["api","search","careers"])
robots("www.google.com",["careers"])
robots("jobs.apple.com",["api","search"])
robots("www.metacareers.com",["jobs","graphql"])
robots("explore.jobs.netflix.net",["api","careers"])
robots("www.uber.com",["api","careers"])
robots("lifeattiktok.com",["api","search"])
robots("careers.ibm.com",["api","search"])
# Amazon count
try:
    r=s.get("https://www.amazon.jobs/en/search.json",params={"base_query":"site reliability engineer","country":"USA","sort":"recent","result_limit":100,"offset":0},timeout=30)
    d=r.json(); jobs=d.get("jobs",[])
    print("AMAZON status",r.status_code,"hits",d.get("hits"),"first dates",[j.get("posted_date") for j in jobs[:5]])
    import datetime
    print("AMAZON sample titles",[j.get("title") for j in jobs[:15]])
except Exception as e: print("AMAZON",e)
# Microsoft eightfold
for url,params in [("https://apply.careers.microsoft.com/api/pcsx/search",{"domain":"microsoft.com","query":"site reliability","location":"United States","start":0,"sort_by":"timestamp"}),
                   ("https://explore.jobs.netflix.net/api/apply/v2/jobs",{"domain":"netflix.com","query":"site reliability","location":"United States","start":0,"num":10,"sort_by":"new"})]:
    try:
        r=s.get(url,params=params,timeout=30); print("EF",url,r.status_code,r.text[:600].replace("\n"," "))
    except Exception as e: print("EF",url,e)
