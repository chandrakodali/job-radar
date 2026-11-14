import requests, json, re
s=requests.Session(); s.headers["User-Agent"]="job-radar/1.0 (personal job search tool; +https://github.com/chandrakodali/job-radar)"
U="https://www-api.ibm.com/search/api/v2"
src=["_id","title","url","description","field_keyword_17","field_keyword_18","field_keyword_19","field_keyword_05","field_keyword_08","field_date_01","field_text_01"]
def q(label, body):
    r=s.post(U,json=body,timeout=30)
    try:
        d=r.json(); h=d.get("hits",{}); hh=h.get("hits",[])
        print("IBM",label,r.status_code,"total",h.get("total"),"n",len(hh),"|",[ (x["_source"].get("title"),x["_source"].get("field_keyword_19")) for x in hh[:3]], "| keys", list((hh[0]["_source"] if hh else {}).items())[:12])
    except Exception: print("IBM",label,r.status_code,r.text[:300])
base={"appId":"careers","scopes":["careers2"],"lang":"zz","_source":src,"sort":[{"_score":"desc"},{"pageviews":"desc"}]}
sq=lambda t: {"bool":{"must":[{"simple_query_string":{"query":t,"fields":["keywords^1","body^1","url^2","description^2","h1s_content^2","title^3","field_text_01"]}}]}}
q("eng100", {**base,"query":sq("engineer"),"size":100,"sm":{"query":"engineer","lang":"zz"}})
q("eng100from100", {**base,"query":sq("engineer"),"size":100,"from":100,"sm":{"query":"engineer","lang":"zz"}})
q("star", {**base,"query":sq("*"),"size":100,"sm":{"query":"*","lang":"zz"}})
q("matchall_sm", {**base,"query":{"bool":{"must":[{"match_all":{}}]}},"size":100,"sm":{"query":"","lang":"zz"}})
q("us_filter", {**base,"query":{"bool":{"must":[{"simple_query_string":{"query":"engineer","fields":["title^3","description^2"]}}],"filter":[{"match_phrase":{"field_keyword_05":"United States"}}]}},"size":100,"sm":{"query":"engineer","lang":"zz"}})
