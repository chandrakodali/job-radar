"""Builds profiles/network and profiles/ot from the DevOps radar's rules (run once; edit the YAML after)."""
import yaml

dev = yaml.safe_load(open("config.yaml"))

SENIOR_EXCL = [
    r"\b(staff|principal|lead|leader|manager|management|director|head of|vp|vice president|architect|distinguished|fellow|chief|supervisor)\b",
    r"\b(iv|v|vi)\b", r"\b(level|lvl)\s*[4-9]\b", r"\b[4-9]\b\s*$",
    r"\b(intern(ship)?|co-?op|summer|phd|apprenticeship program)\b",
    r"\b(fs|ci|full[- ]?scope|lifestyle|counter[- ]?intelligence)\s*[-/]?\s*poly(graph)?\b|\bts/sci\b|\bpoly\b",
    r"\b(clearance|top secret|classified|polygraph)\b",
    r"\b(sales|pre-?sales|account (executive|manager)|business development|recruit\w*|marketing|teacher|instructor|trainer|professor|faculty)\b",
    r"\b(m/w/d|w/m/d|m/f/d|f/m/d|d/f/m|m/f/x|w/m/x|f/h|h/f)\b", r"\bmgr\b",
    r"\b(electrical|power|mechanical|spacecraft|avionics|rf|hardware|propulsion|structural) systems? engineer\b",
    r"\b(red team|penetration|pen ?test\w*|offensive)\b",
    r"\b(help ?desk|desktop support|user support|end[- ]user|service desk)\b",
]
NOT_IT_NETWORK = [
    # healthcare/insurance "provider networks", media networks, social/neural networks, power grids
    r"\b(provider|payer|clinical|physician|pharmacy|dental|vision|behavioral health) network\b",
    r"\bnetwork (contract\w*|relations|development|adequacy|recruit\w*|management (specialist|consultant)|operations (coordinator|representative)|provider|strategy|pricing|reimbursement)\b",
    r"\b(social|neural|marketing|television|tv|broadcast|radio|news|sports|retail|referral|partner|alumni|franchise|supply chain|distribution|logistics|transportation|transit|road|water|gas|pipeline|sewer|power|electric(al)?|transmission|utility|grid|substation) network\b",
    r"\bnetwork(ing)? (event|marketing|sales|growth|partnerships?)\b",
    r"\b(neural|graph) networks?\b", r"\bnetwork science\b",
    r"\b(nurse|nursing|rn|lpn|physician|therapist|pharmacist|medical|clinic|hospital|patient)\b",
    r"\b(cable|fiber|tower|lineman|line worker|splicer|installer|installation technician|field technician|electrician|construction|outside plant|osp)\b",
]
OT_WORDS = (r"\b(it/ot|ot/it|ot (network|networking|security|cyber ?security|systems?|environment|infrastructure|assets?|devices?|"
            r"engineering|domain)|operational technology|ics|industrial control( systems?)?|scada|plc|dcs|purdue|"
            r"iec[- ]?62443|isa[- ]?99|nerc[- ]?cip|industrial (network|networking|ethernet|cyber ?security)|plant floor|"
            r"shop floor|profinet|ethernet/ip|modbus|opc[- ]?ua|dnp3|iec[- ]?61850|claroty|nozomi|dragos)\b")
NET_WORDS = (r"\b(network(ing|s)?|routing|switching|routers?|switches|firewalls?|vlans?|tcp/ip|bgp|ospf|eigrp|mpls|"
             r"sd-?wan|lan/wan|wan|lan|wireless|wlan|cisco|juniper|arista|palo alto|fortinet|fortigate|checkpoint|"
             r"meraki|aruba|f5|dns|dhcp|vpn|ipsec|ccna|ccnp|ccie|jncia|jncis|stratix|industrial ethernet)\b")
NET_EVIDENCE = [
    r"\b(cisco|juniper|arista|aruba|meraki|extreme networks|ubiquiti|ruckus|nexus|catalyst)\b",
    r"\b(routing|switching|routers?|switches|layer ?[23])\b",
    r"\b(bgp|ospf|eigrp|is-is|mpls|stp|vrrp|hsrp|qos)\b",
    r"\b(vlans?|vxlan|subnet\w*|tcp/ip|ipv[46])\b",
    r"\b(firewalls?|palo alto|fortinet|fortigate|checkpoint|asa|ips/ids|vpn|ipsec)\b",
    r"\b(sd-?wan|wan|lan|wlan|wireless|wi-?fi|802\.1x)\b",
    r"\b(dns|dhcp|ipam|load balanc\w+|f5|netscaler)\b",
    r"\b(ccna|ccnp|ccie|jncia|jncis|jncip|network\+|pcnse|nse ?[4-8])\b",
    r"\b(wireshark|solarwinds|prtg|netflow|snmp|packet capture)\b",
]

NETWORK_STRONG = [
    r"\bnetwork(ing)? (engineer|administrator|admin|analyst|specialist|architect associate|technician|consultant|engineering|operations engineer|operations analyst|operations specialist|automation engineer|security engineer|security analyst|support engineer|implementation engineer|deployment engineer|integration engineer|infrastructure engineer|reliability engineer|developer)\b",
    r"\b(lan|wan|lan/wan|wireless|wlan|wi-?fi|routing (and|&) switching|r&s|sd-?wan|data cent(er|re) network(ing)?|cloud network(ing)?|enterprise network(ing)?|campus network|core network|ip network|backbone|edge network|network services) (engineer|administrator|analyst|specialist)\b",
    r"\b(noc|network operations cent(er|re)) (engineer|analyst|technician|specialist|administrator)\b",
    r"\b(firewall|f5|load balancer|dns|ddi|voice and network|unified communications|uc) (engineer|administrator|analyst|specialist)\b",
    r"\b(cisco|juniper|arista|palo alto|fortinet|meraki|aruba) (engineer|administrator|specialist|network engineer)\b",
    r"\bnet(work)?\s*eng(ineer)?\b|\bnetdevops\b|\bnetwork automation\b",
    r"\b(ccna|ccnp|ccie)\b",
]
NETWORK_BRIDGE = [
    r"\b(infrastructure|systems?|it|telecom(munications)?|voice|data cent(er|re)|connectivity|transport|ip|it infrastructure) (engineer|administrator)\b",
    r"\bnetwork\b",
]
# words of networking proper (not "network" in passing, DNS or VPN): one must be in the requirements/duties
NET_CORE = (r"\b(routing|switching|routers?|switches|bgp|ospf|eigrp|mpls|vlans?|vxlan|sd-?wan|cisco|juniper|arista|"
            r"palo alto|fortinet|fortigate|checkpoint|meraki|aruba|firewalls?|ccna|ccnp|ccie|jncia|jncis|wlan|"
            r"wireless lan|load balanc\w+|f5 big-?ip|layer ?[23])\b")
OT_STRONG = [
    r"\b(ot|operational technology|ics|industrial control systems?|scada|industrial|plant|manufacturing|process control|control systems?) (network|networking|infrastructure|network security|cyber ?security|security) (engineer|administrator|analyst|specialist|architect associate|technician|consultant)\b",
    r"\b(ot|operational technology)\b.{0,30}\b(engineer|analyst|specialist|administrator|technician|consultant)\b",
    r"\b(engineer|analyst|specialist|administrator)\b.{0,30}\b(ot|operational technology)\b",
    r"\b(ics|scada|industrial control systems?)\b.{0,30}\b(engineer|analyst|specialist|administrator|technician)\b",
    r"\b(industrial|ot|ics|scada) (cyber ?security|network)\b",
    r"\bnerc[- ]?cip\b",
]
OT_BRIDGE = [
    r"\b(controls?|control systems?|automation|industrial automation|plc|dcs|hmi|instrumentation (and|&) controls?|i&c|process control|substation automation|protection (and|&) control|mes|manufacturing systems?|plant systems?) (engineer|specialist|technician|analyst|programmer|developer|administrator)\b",
    r"\b(network|infrastructure|systems?|cyber ?security|security|firewall) (engineer|administrator)\b",
]


def profile(name, label, file_label, watching, strong, bridge, exclude_extra, required, evidence, wd_terms, wsearch, usajobs_kw):
    cfg = dict(dev)
    cfg["profile"] = {
        "name": name, "label": label,
        "report_url": f"https://github.com/chandrakodali/job-radar/blob/main/REPORT_{name.upper()}.md",
        "matches_file": f"data/{name}/matches.csv", "watching": watching,
        "refresh_note": "Refreshes every hour." if name == "network" else "Refreshes every 2 hours.",
        "score_colors": [70, 55],
        "chips": {"bad": "clearance|citizen|itar|asks for \\d+\\+ years",
                  "good": "entry-level title|0-2 yrs|sponsors visas|e-verify",
                  "warn": "senior title|repost|unclear|contract|staffing|no visa sponsorship|" + file_label},
    }
    cfg["boards"] = {"include_general": True}
    cfg["titles"] = {"strong": strong, "hard_exclude": SENIOR_EXCL + exclude_extra, "senior": dev["titles"]["senior"],
                     "core": [], "bridge": bridge, "exclude": SENIOR_EXCL + exclude_extra, "entry": dev["titles"]["entry"]}
    sc = dict(dev["scoring"])
    sc.update({"bridge_min": 3, "bridge_required": required, "bridge_required_count": 3, "bridge_needs_text": True,
               "bridge_drop": True, "bridge_flag": file_label, "bridge_evidence": evidence})
    cfg["scoring"] = sc
    wd = dict(dev["workday"]); wd["search_terms"] = wd_terms; wd["search_pages"] = 3
    cfg["workday"] = wd
    cfg["ai"] = {"mode": "off"}
    cfg["run"] = {"time_budget_minutes": 28}
    cfg["report"] = {"order": "newest", "days": 7, "hide_years": 5, "track_closed": True, "label": label.replace(" Radar", " jobs in the US"),
                     "note": ("US jobs only. " + watching[0].upper() + watching[1:] + " Jobs asking for 5+ years, or needing a "
                              "clearance or US citizenship, are left out (listed at the end). Senior titles that state no years "
                              "are kept and marked. \"date only\" means the board gives the day but not the time.")}
    cfg["sources"] = {
        "email_alerts": True, "email_days": 4,
        "workable_search": {"every_minutes": 180, "days": 7, "max_pages": 40, "terms": wsearch},
        "hn": {"every_minutes": 360, "days": 7},
        "usajobs": {"every_minutes": 180, "days": 7, "keywords": usajobs_kw, "series": []},
    }
    hdr = (f"# {label}: {watching}\n# Same rules as the DevOps radar (US only; 5+ years, clearance or citizenship left out; senior titles\n"
           "# kept when they ask under 5 years; no-sponsorship and staffing jobs kept and flagged). Built by\n"
           "# scripts/make_network_profiles.py; edit this file directly from now on.\n")
    import os
    os.makedirs(f"profiles/{name}", exist_ok=True)
    open(f"profiles/{name}/config.yaml", "w").write(hdr + yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True, width=200))
    if not os.path.exists(f"profiles/{name}/companies.csv"):
        open(f"profiles/{name}/companies.csv", "w").write("ats,board,company,priority\n")


profile("network", "Network Engineer Radar", "network work in the text",
        "network engineer jobs (network engineer/administrator/analyst, NOC, wireless, firewall, SD-WAN; systems or "
        "infrastructure titles only when the job is mostly networking). OT/industrial network jobs are in the OT radar.",
        NETWORK_STRONG, NETWORK_BRIDGE,
        NOT_IT_NETWORK + [r"\b(ot|operational technology|ics|scada|industrial control|plc|dcs)\b"],
        [NET_CORE], NET_EVIDENCE,
        ["network engineer", "network administrator", "network analyst", "noc", "wireless engineer", "network security"],
        ["network engineer", "network administrator", "network analyst", "noc engineer", "wireless engineer", "firewall engineer"],
        ["network engineer", "network administrator", "network specialist"])

profile("ot", "OT Network Engineer Radar", "OT network work in the text",
        "OT network engineer jobs (OT/ICS/SCADA network, infrastructure and OT security roles; controls, automation or "
        "network titles only when the job is about networks in an OT/industrial setting).",
        OT_STRONG, OT_BRIDGE,
        [r"\b(provider|payer|clinical|social|neural|marketing|television|broadcast) network\b",
         r"\b(occupational therap\w+|physical therap\w+|\bot/pt\b|nurse|nursing|clinical|patient|rehab\w*|school|teacher)\b",
         r"\b(overtime|ot hours)\b"],
        [OT_WORDS, NET_CORE], NET_EVIDENCE + [OT_WORDS],
        ["OT network", "operational technology", "SCADA", "ICS", "industrial network", "controls engineer", "OT security"],
        ["OT network engineer", "operational technology", "SCADA engineer", "ICS engineer", "industrial network", "OT security"],
        ["operational technology", "SCADA", "industrial control systems"])
print("ok")
