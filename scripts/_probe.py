import requests, json, time, re
UA="job-radar/1.0 (personal job search tool; +https://github.com/chandrakodali/job-radar)"
s=requests.Session(); s.headers["User-Agent"]=UA
def get(url, **kw):
    try:
        r=s.get(url,timeout=30,**kw); return r
    except Exception as e: print("ERR",url,e); return None
def around(t, pats, w=500, k=3):
    for p in pats:
        for m in list(re.finditer(p, t))[:k]:
            print("   ~",p,":",re.sub(r"\s+"," ",t[max(0,m.start()-150):m.start()+w]))
def show(url, pats=(), n=600, **kw):
    r=get(url, **kw)
    if r is None: return None
    t=r.text; print("GET",url,r.status_code,r.headers.get("content-type","")[:40],len(t),"|",re.sub(r"\s+"," ",t[:n]))
    around(t, pats); return r
# Zoho
r=show("https://3rtalent.zohorecruit.com/jobs/Careers", [r'id="jobs"', r'jobs\s*[:=]\s*\[', r'Job_Opening_Name', r'"Posting_Title"', r'application/ld\+json'], 200)
show("https://agileengine.zohorecruit.com/jobs/Careers", [r'Posting_Title', r'id="jobs"'], 200)
# CATS
r=show("https://3kc.catsone.com/careers/2010", [r'__NEXT_DATA__', r'"jobs"\s*:', r'"title"\s*:', r'/careers/2010-'], 200)
show("https://sitemap.catsone.com/index.php?m=sitemap&a=sitemap&siteID=6149", [r'<loc>'], 600)
# Keka
show("https://acuitilabs.keka.com/careers", [r'embedjobs', r'api/', r'careers/api'], 300)
show("https://acuitilabs.keka.com/robots.txt", [], 400)
show("https://3scsolution.keka.com/careers/api/embedjobs/6f255cd1-b94d-4a2e-a30c-6c75aced8f3a", [], 1200)
# Homerun
show("https://acato.homerun.co/", [r'application/ld\+json', r'__NEXT_DATA__', r'window\.__', r'"vacancies"', r'"jobs"'], 300)
show("https://acato.homerun.co/robots.txt", [], 300)
# Factorial
show("https://avvoka.factorialhr.com/", [r'job_posting', r'application/ld\+json', r'__NEXT_DATA__', r'"job_postings"'], 300)
show("https://avvoka.factorialhr.com/robots.txt", [], 300)
# CareerPuck
show("https://app.careerpuck.com/job-board/academia", [r'__NEXT_DATA__', r'api\.careerpuck', r'"jobs"', r'"title"'], 300)
for u in ["https://api.careerpuck.com/v1/public/job-boards/academia","https://app.careerpuck.com/api/public/job-boards/academia","https://app.careerpuck.com/api/job-board/academia"]:
    show(u, [], 500)
# JOIN
show("https://join.com/companies/02100", [r'__NEXT_DATA__', r'"jobs"\s*:', r'"title"\s*:'], 100)
# TriNet
show("https://app.trinethire.com/", [r'href="[^"]*companies', r'careers'], 300)
# YC jobs
r=show("https://www.ycombinator.com/jobs", [r'data-page=', r'"jobPostings"', r'"title"\s*:', r'/companies/[a-z0-9-]+/jobs/'], 100)
show("https://www.ycombinator.com/jobs/role/software-engineer", [r'data-page=', r'"jobPostings"'], 100)
