"""IT Entry-Level Radar (profiles/freshers): any IT title, kept only when the job shows it's entry level."""
import os
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from radar.match import Rules, assess, title_category
from radar.models import Board, Job

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES = Rules.from_config(yaml.safe_load(open(os.path.join(ROOT, "profiles", "freshers", "config.yaml"))))
NOW = datetime.now(timezone.utc)

KEEP = ["Software Engineer", "Junior Software Engineer", "Software Engineer I", "Associate Software Developer",
        "New Grad Software Engineer 2026", "Data Analyst", "Data Engineer", "QA Analyst", "Test Automation Engineer",
        "IT Support Specialist", "Help Desk Technician", "Service Desk Analyst", "Desktop Support Technician",
        "Network Engineer", "Network Administrator", "SOC Analyst", "Cybersecurity Analyst", "Cloud Engineer",
        "DevOps Engineer", "Database Administrator", "Business Analyst", "Systems Analyst", "Programmer Analyst",
        "Web Developer", "Full Stack Developer", "iOS Developer", "Machine Learning Engineer", "Salesforce Developer",
        "SAP Consultant", "Application Support Analyst", "Implementation Specialist", "Technology Analyst",
        "Data Center Technician", "Systems Administrator", "BI Developer", "Technical Support Engineer",
        "Associate Consultant - Technology", "Graduate Software Engineer", "Financial Systems Analyst"]
DROP = ["Software Engineer II", "Senior Software Engineer", "Sr. Data Analyst", "Lead Developer", "IT Manager",
        "Software Engineering Intern", "Mechanical Engineer", "Electrical Engineer I", "Registered Nurse",
        "Sales Engineer", "Financial Analyst", "Accountant", "Warehouse Associate", "Customer Service Representative",
        "Software Engineer 2", "Data Scientist III", "Staff Engineer", "Solutions Architect", "Recruiter",
        "Hardware Design Engineer", "Credit Analyst", "Retail Sales Associate", "Teacher"]


@pytest.mark.parametrize("title", KEEP)
def test_it_titles_kept(title):
    assert title_category(title, RULES) == "core"


@pytest.mark.parametrize("title", DROP)
def test_other_titles_dropped(title):
    assert title_category(title, RULES) is None


@pytest.mark.parametrize("title,desc,want", [
    ("Software Engineer", "We are hiring new grads! Bachelor's in CS. Build APIs in Java.", "keep"),
    ("Software Engineer", "Requirements: 3+ years of experience building web services.", "blocked"),
    ("Software Engineer", "Requirements: Bachelor's degree. Experience with Python.", "old"),   # nothing says entry level
    ("Software Engineer", "Qualifications: 0-2 years of experience in software development.", "keep"),
    ("Software Engineer", "Requirements: 1+ years of experience with React.", "keep"),
    ("Software Engineer", "Requirements: 2+ years of experience with React.", "blocked"),
    ("Junior Data Analyst", "Requirements: SQL, Excel.", "keep"),
    ("Data Analyst", "This is an entry-level position. SQL and Tableau.", "keep"),
    ("Associate Software Engineer", "Requirements: US citizenship required. Active secret clearance.", "blocked"),
    ("Software Engineer I", "", "keep"),
])
def test_entry_level_evidence(title, desc, want):
    j = Job(ats="greenhouse", board="acme", job_id="1", company="Acme", title=title, location="Austin, TX", url="u",
            posted_at=NOW - timedelta(hours=2), description=desc)
    a = assess(j, Board("greenhouse", "acme", "Acme"), RULES, "core", now=NOW)
    assert ("keep" if a.status in ("alert", "quiet") else a.status) == want
