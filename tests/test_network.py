"""Network Engineer and OT Network Engineer radars (profiles/network, profiles/ot)."""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from radar.match import Rules, assess, title_category
from radar.models import Board, Job

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load = lambda n: Rules.from_config(yaml.safe_load(open(os.path.join(ROOT, "profiles", n, "config.yaml"))))
NET, OT = load("network"), load("ot")
NOW = datetime.now(timezone.utc)
sys.path.insert(0, "/tmp")

NET_TITLES = {
    "Network Engineer": "core", "Network Engineer II": "core", "Senior Network Engineer": "core",
    "Network Administrator": "core", "Network Analyst": "core", "Junior Network Engineer": "core",
    "Network Security Engineer": "core", "Network Automation Engineer": "core", "WLAN Engineer": "core",
    "NOC Engineer": "core", "Firewall Engineer": "core", "SD-WAN Engineer": "core", "LAN/WAN Engineer": "core",
    "Cloud Network Engineer": "core", "Network Technician": "core", "Cisco Network Engineer": "core",
    "Infrastructure Engineer": "bridge", "Systems Engineer": "bridge", "Telecommunications Engineer": "bridge",
    "IT Support Specialist": None, "Help Desk Technician": None, "Desktop Support Engineer": None,
    "Senior Red Team Operator, Network": None, "System Engineer (f/m/d) IP Transport": None,
    "Network Architect": None, "Lead Network Engineer": None, "Network Manager": None, "Network Engineer IV": None,
    "Network Engineer - TS/SCI": None, "Network Engineer Intern": None, "Provider Network Manager": None,
    "Network Contract Specialist": None, "Network Relations Consultant": None, "Social Network Analyst": None,
    "Neural Network Engineer": None, "Network Sales Engineer": None, "Fiber Network Technician": None,
    "OT Network Engineer": None, "SCADA Network Engineer": None, "Transmission Network Engineer": None,
}
OT_TITLES = {
    "OT Network Engineer": "core", "OT Engineer": "core", "Operational Technology Engineer": "core",
    "OT Security Engineer": "core", "OT Cybersecurity Analyst": "core", "ICS Network Engineer": "core",
    "SCADA Engineer": "core", "Industrial Network Engineer": "core", "Plant Network Engineer": "core",
    "Senior OT Network Engineer": "core", "NERC CIP Analyst": "core",
    "Controls Engineer": "bridge", "Automation Engineer": "bridge", "PLC Engineer": "bridge",
    "Network Engineer": "bridge", "Substation Automation Engineer": "bridge", "Cybersecurity Engineer": "bridge",
    "Spacecraft Power and Electrical Systems Engineer": None, "Information Security Specialist": None, "IT Engineer": None,
    "Occupational Therapist": None, "Occupational Therapist (OT)": None, "OT - Outpatient Therapist": None,
    "OT Manager": None, "Lead OT Engineer": None, "OT Architect": None, "SCADA Engineer - TS/SCI": None,
    "OT Engineer Intern": None, "School OT": None, "OT Sales Engineer": None,
}


@pytest.mark.parametrize("title,want", NET_TITLES.items())
def test_network_titles(title, want):
    assert title_category(title, NET) == want


@pytest.mark.parametrize("title,want", OT_TITLES.items())
def test_ot_titles(title, want):
    assert title_category(title, OT) == want


PAD = " We value teamwork, ownership and clear communication." * 6
NET_JOB = "Requirements:\n- 2+ years configuring Cisco routers and switches, BGP/OSPF, VLANs and firewalls\n- CCNA" + PAD
SYS_JOB = "Requirements:\n- 2+ years administering Windows Server, Active Directory and VMware" + PAD
PLC_JOB = "Requirements:\n- 2+ years programming Allen-Bradley PLCs and HMIs in a manufacturing plant" + PAD
OT_NET_JOB = ("Requirements:\n- 2+ years with industrial Ethernet: Cisco IE switches, VLANs and firewalls on the plant floor\n"
              "- SCADA and PLC networks, Purdue model, IEC 62443" + PAD)
IT_NET_JOB = "Requirements:\n- 2+ years with Cisco switches, VLANs, firewalls for our corporate offices" + PAD


def judge(rules, title, cat, desc):
    j = Job(ats="greenhouse", board="acme", job_id="1", company="Acme", title=title, location="Houston, TX",
            url="u", posted_at=NOW - timedelta(hours=3), description=desc)
    a = assess(j, Board("greenhouse", "acme", "Acme"), rules, cat, now=NOW)
    return ("keep" if a.status in ("alert", "quiet") else a.status), a


@pytest.mark.parametrize("title,cat,desc,want", [
    ("Network Engineer", "core", NET_JOB, "keep"),
    ("Network Engineer", "core", NET_JOB.replace("2+", "6+"), "blocked"),
    ("Network Engineer", "core", "Must hold an active Secret clearance. " + NET_JOB, "blocked"),
    ("Senior Network Engineer", "core", "Design our WAN.", "keep"),              # no years: kept, marked
    ("Systems Engineer", "bridge", NET_JOB, "keep"),                            # mostly networking
    ("Systems Engineer", "bridge", SYS_JOB, "old"),                             # not networking: dropped
    ("Systems Engineer", "bridge", SYS_JOB + " Basic knowledge of networking, DNS, DHCP and VPN.", "old"),
    ("Infrastructure Engineer", "bridge", "", "old"),                           # can't tell without the text
])
def test_network_job_text(title, cat, desc, want):
    got, a = judge(NET, title, cat, desc)
    assert got == want, a.flags


@pytest.mark.parametrize("title,cat,desc,want", [
    ("OT Network Engineer", "core", OT_NET_JOB, "keep"),
    ("OT Network Engineer", "core", OT_NET_JOB.replace("2+", "7+"), "blocked"),
    ("Controls Engineer", "bridge", OT_NET_JOB, "keep"),                        # controls job about OT networks
    ("Controls Engineer", "bridge", PLC_JOB, "old"),                            # PLC programming only: dropped
    ("Network Engineer", "bridge", OT_NET_JOB, "keep"),                         # network job on the plant floor
    ("Network Engineer", "bridge", IT_NET_JOB, "old"),                          # office IT network: not OT
    ("Network Engineer", "bridge", IT_NET_JOB + " Honeywell access control. OT required on weekends.", "old"),
])
def test_ot_job_text(title, cat, desc, want):
    got, a = judge(OT, title, cat, desc)
    assert got == want, a.flags


def test_same_job_on_two_boards_is_one_row():
    from radar.report import collapse
    base = {"title": "Infrastructure Engineer II", "location": "Phoenix, AZ, United States", "posted": "2026-11-18T00:00:00+00:00",
            "_first_seen": NOW, "ats": "oracle", "score": 60, "status": "quiet"}
    rows, merged = collapse([{**base, "company": "American Express", "uid": "a"}, {**base, "company": "Egug Fa Us2 Oracle", "uid": "b"}])
    assert len(rows) == 1 and merged == 1
