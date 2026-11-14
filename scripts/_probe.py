import requests, json, re
s=requests.Session(); s.headers["User-Agent"]="job-radar/1.0 (personal job search tool; +https://github.com/chandrakodali/job-radar)"
def p(label, r, n=900):
    try: print(label, r.status_code, r.headers.get("content-type","")[:30], re.sub(r"\s+"," ",r.text[:n]))
    except Exception as e: print(label, "ERR", e)
# Amazon keys
r=s.get("https://www.amazon.jobs/en/search.json",params={"base_query":"","country":"USA","sort":"recent","result_limit":3,"offset":0},timeout=30)
d=r.json(); print("AMZ hits",d.get("hits")); j=(d.get("jobs") or [{}])[0]; print("AMZ keys",{k:(str(v)[:60]) for k,v in j.items()})
# Apple
for u in ["https://jobs.apple.com/api/v1/CSRFToken","https://jobs.apple.com/api/csrfToken"]:
    r=s.get(u,timeout=30); p("APPLE "+u, r, 300); print("  hdr", {k:v for k,v in r.headers.items() if "csrf" in k.lower()})
tok=r.headers.get("x-apple-csrf-token") or ""
body={"query":"devops","filters":{"locations":["postLocation-USA"]},"page":1,"locale":"en-us","sort":"newest","format":{"longDate":"MMMM D, YYYY","mediumDate":"MMM D, YYYY"}}
for u in ["https://jobs.apple.com/api/v1/search","https://jobs.apple.com/api/role/search"]:
    r=s.post(u,json=body,headers={"x-apple-csrf-token":tok,"Origin":"https://jobs.apple.com","Referer":"https://jobs.apple.com/en-us/search"},timeout=30); p("APPLE POST "+u, r, 700)
r=s.get("https://jobs.apple.com/en-us/search",params={"search":"devops","location":"united-states-USA","sort":"newest"},timeout=30); p("APPLE PAGE", r, 200)
m=re.search(r'window\.__staticRouterHydrationData = JSON\.parse\("(.{0,600})', r.text); print("  hydration:", m.group(1)[:600] if m else None)
# IBM
r=s.get("https://www-api.ibm.com/robots.txt",timeout=20); p("IBM API ROBOTS", r, 300)
body={"appId":"careers","scopes":["careers2"],"query":{"bool":{"must":[{"simple_query_string":{"query":"devops","fields":["keywords^1","body^1","url^2","description^2","h1s_content^2","title^3","field_text_01"]}}]}},"size":5,"sort":[{"_score":"desc"},{"pageviews":"desc"}],"lang":"zz","localeSelector":{},"sm":{"query":"devops","lang":"zz"},"_source":["_id","title","url","description","language","entitled","field_keyword_17","field_keyword_08","field_keyword_18","field_keyword_19"]}
r=s.post("https://www-api.ibm.com/search/api/v2",json=body,timeout=30); p("IBM SEARCH", r, 900)
# TikTok
r=s.get("https://api.lifeattiktok.com/robots.txt",timeout=20); p("TT API ROBOTS", r, 300)
body={"recruitment_id_list":[],"job_category_id_list":[],"subject_id_list":[],"location_code_list":["CT_1103355"],"keyword":"devops","limit":5,"offset":0}
r=s.post("https://api.lifeattiktok.com/api/v1/public/supplier/search/job/posts",json=body,headers={"website-path":"tiktok","Origin":"https://lifeattiktok.com","Referer":"https://lifeattiktok.com/"},timeout=30); p("TT SEARCH", r, 900)
