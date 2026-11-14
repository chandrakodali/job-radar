"""Java Developer Radar (profiles/java): Java titles (never JavaScript), generic backend titles only when
the requirements need Java, and the DevOps radar's years / clearance / senior rules."""
import os
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from radar.match import Rules, assess, title_category
from radar.models import Board, Job

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES = Rules.from_config(yaml.safe_load(open(os.path.join(ROOT, "profiles", "java", "config.yaml"))))
NOW = datetime.now(timezone.utc)

TITLES = {
    "Java Developer": "core", "Java Software Engineer": "core", "Senior Java Developer": "core",
    "Sr. Java Engineer": "core", "Java Full Stack Developer": "core", "Full Stack Java Developer": "core",
    "Java Backend Engineer": "core", "Software Engineer - Java": "core", "Java/J2EE Developer": "core",
    "J2EE Developer": "core", "Spring Boot Developer": "core", "Java Microservices Developer": "core",
    "Java Engineer II": "core", "Associate Java Developer": "core", "Junior Java Developer": "core",
    "Java Developer III": "core", "Java/JavaScript Developer": "core", "Java AWS Developer": "core",
    "Java Developer (Contract)": "core", "Core Java Developer": "core", "Backend Developer (Java)": "core",
    "Software Engineer": "bridge", "Backend Engineer": "bridge", "Software Developer II": "bridge",
    "Full Stack Engineer": "bridge", "Application Developer": "bridge", "Senior Software Engineer": "bridge",
    "API Developer": "bridge", "Programmer Analyst": "bridge", "Kotlin Developer": "bridge",
    "JavaScript Developer": None, "Java Script Developer": None, "Java Architect": None, "Lead Java Developer": None,
    "Java Tech Lead": None, "Principal Java Engineer": None, "Staff Software Engineer": None,
    "Java Developer Intern": None, "Java Instructor": None, "Java Developer IV": None, "Java Developer - TS/SCI": None,
    "Barista - Java House": None, "Java QA Tester": None, "Python Developer": None, ".NET Developer": None,
    "Android Developer": None, "Frontend Engineer": None, "React Developer": None, "Engineering Manager, Java": None,
    "Node.js Backend Engineer": None, "QA Automation Engineer": None, "Machine Learning Engineer": None,
}


@pytest.mark.parametrize("title,want", TITLES.items())
def test_titles(title, want):
    assert title_category(title, RULES) == want


JAVA_REQ = ("About the role: build services.\nRequirements:\n- 2+ years of experience with Java and Spring Boot\n"
            "- REST APIs, Hibernate, Maven\n" + "We value teamwork and ownership. " * 10)
INCIDENTAL = ("Requirements:\n- 3+ years of experience with Python or Go\n- AWS, Docker\n"
              "Nice to have: exposure to Java.\n" + "We value teamwork and ownership. " * 10)
NO_JAVA = "Requirements:\n- 2+ years with Node.js and TypeScript\n" + "We value teamwork. " * 20


@pytest.mark.parametrize("title,cat,desc,want", [
    ("Java Developer", "core", "Requirements: 3+ years of Java.", "keep"),
    ("Java Developer", "core", "Requirements: 6+ years of Java development.", "blocked"),
    ("Senior Java Developer", "core", "Build microservices.", "keep"),                    # no years stated: kept, marked
    ("Java Developer", "core", "Must be a U.S. citizen with an active Secret clearance.", "blocked"),
    ("Software Engineer", "bridge", JAVA_REQ, "keep"),                                   # requirements need Java
    ("Software Engineer", "bridge", INCIDENTAL, "old"),                                  # Java only a nice-to-have
    ("Backend Engineer", "bridge", NO_JAVA, "old"),
    ("Software Engineer", "bridge", "", "old"),                                          # can't tell without the text
    ("Software Engineer", "bridge", JAVA_REQ.replace("2+", "5+"), "blocked"),
])
def test_job_text(title, cat, desc, want):
    j = Job(ats="greenhouse", board="acme", job_id="1", company="Acme", title=title, location="Austin, TX", url="u",
            posted_at=NOW - timedelta(hours=3), description=desc)
    a = assess(j, Board("greenhouse", "acme", "Acme"), RULES, cat, now=NOW)
    assert ("keep" if a.status in ("alert", "quiet") else a.status) == want, a.flags
    if want == "keep" and cat == "bridge":
        assert "requires Java" in a.flags


from radar.match import classify_location  # noqa: E402


@pytest.mark.parametrize("place,want", [
    ("Brasil", "non_us"), ("São Paulo, Brasil", "non_us"), ("Ciudad de México", "non_us"), ("München", "non_us"),
    ("España", "non_us"), ("Zürich", "non_us"), ("Warszawa", "non_us"), ("Montréal", "non_us"), ("Remote, LATAM", "non_us"),
    ("Berlin, DE", "non_us"), ("Toronto, CA", "non_us"), ("Delhi, IN", "non_us"),
    ("Dublin, CA", "us"), ("Rome, GA", "us"), ("Birmingham, AL", "us"), ("Paris, TX", "us"), ("New Mexico", "us"),
    ("Albuquerque, New Mexico", "us"), ("Austin, TX", "us"), ("Remote - US", "us"),
])
def test_locations(place, want):
    assert classify_location(place, []) == want
