import sys, os, time, re, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from radar import ats as A
from radar.models import Board
from radar import sources as S
import requests
def t(ats, board, detail=True):
    b=Board(ats=ats, board=board, company="")
    try:
        jobs=A.LISTERS[ats](b)
        j=jobs[0] if jobs else None
        print("READER",ats,board,"jobs",len(jobs),"| first:",(j.title,j.location,j.posted_at,j.url,j.company) if j else None)
        if j and detail and ats in A.DETAILERS:
            A.DETAILERS[ats](j); print("   detail:",j.title,"|",j.location,"|",j.posted_at,"|",(j.description or "")[:160].replace("\n"," "))
    except Exception as e: print("READER",ats,board,"ERR",type(e).__name__,str(e)[:200])
t("zoho","agileengine"); t("zoho","3rtalent")
t("personio","1komma5grad.jobs.personio.de")
t("catsone","3kc")
t("careerpuck","academia")
t("join","02100")
t("homerun","acato")
t("eightfold","apply.careers.microsoft.com|microsoft.com")
t("eightfold","explore.jobs.netflix.net|netflix.com")
try:
    yc=S.yc_jobs({"roles":["software-engineer","devops"]}); print("YC",len(yc), [(j.company,j.title,j.location,j.url) for j in yc[:3]])
    if yc: A.DETAILERS["yc"](yc[0]); print("   yc detail:", yc[0].posted_at, (yc[0].description or "")[:200].replace("\n"," "))
except Exception as e: print("YC ERR",e)
s=requests.Session(); s.headers["User-Agent"]="job-radar/1.0 (personal job search tool; +https://github.com/chandrakodali/job-radar)"
for h in ["www.amazon.jobs","jobs.apple.com","www.metacareers.com","careers.ibm.com","lifeattiktok.com","apply.careers.microsoft.com","explore.jobs.netflix.net"]:
    try:
        r=s.get(f"https://{h}/robots.txt",timeout=20); lines=[l for l in r.text.splitlines() if l.lower().startswith(("disallow","user-agent"))]
        print("ROBOTS",h,r.status_code,"|"," ; ".join(lines[:25])[:900])
    except Exception as e: print("ROBOTS",h,e)
try:
    r=s.get("https://www.amazon.jobs/en/search.json",params={"base_query":"devops","country":"USA","sort":"recent","result_limit":10},timeout=30)
    d=r.json(); print("AMAZON",r.status_code,d.get("hits"),[ (j.get("title"),j.get("posted_date"),j.get("location")) for j in d.get("jobs",[])[:3]])
except Exception as e: print("AMAZON ERR",e)
