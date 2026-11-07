from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tallo.parsing import (
    classify_authorization,
    classify_location,
    classify_title,
    extract_years,
    normalize_title,
    parse_posted,
    requires_current_enrollment,
    score_job,
)


NOW = datetime(2026, 11, 7, 19, 15, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("just now", NOW),
        ("  Posted: TODAY. ", datetime(2026, 11, 7, 6, tzinfo=UTC)),
        ("posted 1 minute ago", NOW - timedelta(minutes=1)),
        ("POSTED : 45   minutes ago!", NOW - timedelta(minutes=45)),
        ("2 hours ago", NOW - timedelta(hours=2)),
        ("yesterday", datetime(2026, 11, 6, 6, tzinfo=UTC)),
        ("3 days ago", NOW - timedelta(days=3)),
        ("1 week ago", NOW - timedelta(weeks=1)),
        ("2 weeks ago", NOW - timedelta(weeks=2)),
        ("30+ days ago", NOW - timedelta(days=30)),
        ("Nov 5, 2026", datetime(2026, 11, 5, 6, tzinfo=UTC)),
        ("Nov. 5, 2026", datetime(2026, 11, 5, 6, tzinfo=UTC)),
        ("11/05/2026", datetime(2026, 11, 5, 6, tzinfo=UTC)),
        ("2026-11-05", datetime(2026, 11, 5, 6, tzinfo=UTC)),
        ("2026-10-31", datetime(2026, 10, 31, 5, tzinfo=UTC)),
        ("Last Updated: 1 day ago", None),
        ("Posted: unknown", None),
        ("Not specified", None),
        ("", None),
        ("1000000000000 days ago", None),
    ],
)
def test_posted_date_formats(text, expected):
    assert parse_posted(text, NOW) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Minimum 3+ years of experience with cloud systems.", 3),
        ("3 plus years of practical infrastructure experience.", 3),
        ("3-5 years of experience.", 3),
        ("3 to 5 years of software engineering exp.", 3),
        ("3–5 yrs of professional experience.", 3),
        ("Experience: at least 2 years in a relevant role.", 2),
        ("Minimum of 2 years of operating systems experience.", 2),
        ("2 years' experience required.", 2),
        ("1 year of work experience.", 1),
        ("0-2 years of experience.", 0),
        ("Entry level role.", 0),
        ("three years of work experience", 3),
        ("five (5) years of work experience", 5),
        ("five(5) years of work experience", 5),
        ("Fifteen years of experience", 15),
        ("two to four years of experience", 2),
        ("Required Experience\n- 3 years maintaining servers", 3),
        ("Basic Qualifications: 2 yrs exp. Preferred Qualifications: 5 years of experience.", 2),
        ("Required: 2 years of experience and 4 years of network experience.", 4),
        ("3 years of experience with Linux or 5 years of experience with Windows", 3),
        ("Bachelor's + 4 years OR Master's + 2 years", 4),
        ("Master's + 2 years or Bachelor's + 4 years", 4),
        ("4 years of experience with a Bachelor's or 2 years with a Master's", 4),
        ("Bachelor's degree plus four years OR Master's degree plus two years", 4),
        ("B.S. Degree + 4 years of experience OR M.S. Degree + 2 years of experience", 4),
        ("Master's + 2 years OR 5 years of professional experience", 5),
        ("Preferred Qualifications\n- 9 years of cloud experience", None),
        ("3 years of experience with Kubernetes preferred.", None),
        ("We have over 30 years in business and value experience.", None),
        ("Our company has over 30 years of experience.", None),
        ("A 2 year appointment with renewable terms.", None),
        ("2 years of experience. 30 years in business.", 2),
    ],
)
def test_years_requirements(text, expected):
    assert extract_years(text) == expected


def test_qualification_text_fixture():
    text = (Path(__file__).parent / "fixtures" / "qualification_paths.txt").read_text(encoding="utf-8")
    assert extract_years(text) == 4


@pytest.mark.parametrize(
    "text",
    [
        "Must hold a security clearance.",
        "Secret required.",
        "Secret",
        "Clearance: Secret",
        "Top Secret",
        "TS/SCI",
        "Public Trust",
        "DoD",
        "Active clearance",
        "Ability to obtain a clearance",
        "Must be a U.S. citizen.",
        "U.S. citizenship is required.",
        "This position requires U.S. citizenship.",
        "US Citizens Only",
        "U.S. citizens and permanent residents only",
        "Only U.S. citizens or permanent residents may apply.",
        "U.S. person only",
        "U.S. persons only",
        "ITAR",
        "EAR",
        "ear",
        "ear export control requirements",
        "Green card holders only.",
    ],
)
def test_blocked_authorization_signals(text):
    result = classify_authorization("DevOps Engineer", text)
    assert result.blocked
    assert not result.no_sponsorship


@pytest.mark.parametrize(
    "text",
    [
        "No clearance required.",
        "Security clearance not required.",
        "Top Secret clearance is not required.",
        "Top Secret security clearance not required.",
        "Secret is not required.",
        "This role does not require a clearance.",
        "Security clearance: no clearance required.",
        "No clearance required (Secret and TS/SCI).",
        "Manage secret rotation and cloud secrets.",
    ],
)
def test_clearance_exceptions(text):
    assert not classify_authorization("Cloud Engineer", text).blocked


def test_clearance_exceptions_are_scoped():
    assert classify_authorization(
        "DevOps Engineer", "No clearance required. US citizenship is required."
    ).blocked
    assert classify_authorization(
        "DevOps Engineer", "No clearance required for A, but active clearance is required for B."
    ).blocked
    assert classify_authorization(
        "DevOps Engineer", "No clearance required for A, active clearance is required for B."
    ).blocked
    title = "DevOps Engineer - Top Secret clearance is not required"
    assert not classify_authorization(title, "").blocked
    assert not classify_title(title).clearance_excluded


@pytest.mark.parametrize(
    "text",
    [
        "Unable to sponsor",
        "We are not able to sponsor",
        "Cannot sponsor",
        "Will not sponsor",
        "We are unable to provide visa sponsorship",
        "No visa sponsorship",
        "Sponsorship is not available",
        "Must be authorized to work in the US without sponsorship (now or in the future)",
        "Must work without the need for employer sponsorship",
        "No OPT",
        "No CPT",
        "No OPT/CPT",
        "No F-1 OPT",
        "No STEM OPT",
        "No H-1B",
        "No H1B transfers",
        "No H-1B sponsorship",
    ],
)
def test_no_sponsorship_signals_remain_nonblocked(text):
    result = classify_authorization("DevOps Engineer", text)
    assert not result.blocked
    assert result.no_sponsorship
    assert "no sponsorship" in result.flags
    assert not result.opt_friendly
    assert not result.sponsorship


@pytest.mark.parametrize(
    ("text", "flag"),
    [
        ("E-Verify", "E-Verify"),
        ("OPT/CPT welcome", "OPT-friendly"),
        ("F-1 OPT", "OPT-friendly"),
        ("STEM OPT", "OPT-friendly"),
        ("Visa sponsorship available", "sponsorship available"),
        ("Will sponsor", "sponsorship available"),
        ("H-1B sponsorship", "sponsorship available"),
    ],
)
def test_positive_authorization_signals(text, flag):
    result = classify_authorization("DevOps Engineer", text)
    assert flag in result.flags
    assert not result.no_sponsorship
    assert result.opt_friendly or result.sponsorship


def test_c2c_w2_and_staffing_flags():
    result = classify_authorization(
        "Technical Support Engineer",
        "Only US citizens and green card holders may apply via C2C. W-2 only for direct hires.",
        "Example Consulting",
    )
    assert not result.blocked
    assert not result.no_sponsorship
    assert set(result.flags) == {"W2 only", "C2C restriction", "staffing firm: verify"}
    assert classify_authorization("SRE", "US citizenship required. No C2C.").blocked


@pytest.mark.parametrize(
    ("title", "family", "level", "clearance"),
    [
        ("Senior DevOps", None, None, False),
        ("Sr. DevOps Eng.", None, None, False),
        ("DevOps Engineer I", "core", 1, False),
        ("SRE II", "core", 2, False),
        ("Cloud Support Engineer", "core", None, False),
        ("Cloud Support Engineer Level 2", "core", 2, False),
        ("SRE III", None, 3, False),
        ("SRE 3", None, 3, False),
        ("SRE IV", None, 4, False),
        ("SRE Level 5", None, 5, False),
        ("Systems Engineer (Missile Defense)", None, None, False),
        ("Systems Engineer (Network Automation)", "bridge", None, False),
        ("Maintenance Reliability Engineer", None, None, False),
        ("Jr. Linux SysAdmin", "bridge", 1, False),
        ("Network Eng. — Early Career", "bridge", 1, False),
        ("DevOps Engineer (TS/SCI)", "core", None, True),
        ("DevOps Internship", None, None, False),
        ("IT Support Engineer Co-op", None, None, False),
        ("Platform Engineer Summer Program", None, None, False),
        ("Technical Support Engineer", "bridge", None, False),
        ("Data Center Technician", "bridge", None, False),
        ("24/7 NOC Engineer", "bridge", None, False),
        ("NOC Engineer (24 Hour Shift)", "bridge", None, False),
        ("Data Center Technician 3+", None, 3, False),
    ],
)
def test_title_match_and_level(title, family, level, clearance):
    result = classify_title(title)
    assert (result.family, result.level, result.clearance_excluded) == (family, level, clearance)


def test_title_normalization_and_keyword_overrides():
    assert normalize_title(" Sr.  SysAdmin — IT   Eng. ") == "senior systems administrator - it engineer"
    assert classify_title("Production Engineering", {"core": [r"\bproduction engineering\b"]}).family == "core"
    assert requires_current_enrollment("DevOps Apprentice", "Applicants must be currently enrolled.")
    assert not requires_current_enrollment("DevOps Engineer", "Help apprentices who are currently enrolled.")
    assert not requires_current_enrollment("DevOps Apprentice", "Graduates welcome.")
    assert not requires_current_enrollment("DevOps Apprentice", "Applicants are not required to be currently enrolled.")


@pytest.mark.parametrize(
    ("location", "is_us", "unclear", "work_type"),
    [
        ("Tampa, FL (Onsite)", True, False, "Onsite"),
        ("Salem, OR", True, False, ""),
        ("Washington, D.C. (Hybrid)", True, False, "Hybrid"),
        ("Washington, DC", True, False, ""),
        ("North Carolina", True, False, ""),
        ("Austin", True, False, ""),
        ("Remote - US", True, False, "Remote"),
        ("Remote (US)", True, False, "Remote"),
        ("Remote (United States)", True, False, "Remote"),
        ("Remote (u.s.)", True, False, "Remote"),
        ("Remote", True, True, "Remote"),
        ("Remote (Canada)", False, False, "Remote"),
        ("Remote (Toronto, ON)", False, False, "Remote"),
        ("Remote - non-US", False, False, "Remote"),
        ("United States", True, False, ""),
        ("USA", True, False, ""),
        ("CA", False, False, ""),
        ("IN", False, False, ""),
        ("DE", False, False, ""),
        ("IL", False, False, ""),
        ("Sacramento, CA", True, False, ""),
        ("Indianapolis, IN", True, False, ""),
        ("Wilmington, DE", True, False, ""),
        ("Chicago, IL", True, False, ""),
        ("London, UK; Austin, TX", True, False, ""),
        ("London, UK, Austin, TX", True, False, ""),
        ("London, UK, Austin, Texas", True, False, ""),
        ("Toronto, ON, Austin, TX", True, False, ""),
        ("Munich, DE, Germany", False, False, ""),
        ("Ontario, CA", True, False, ""),
        ("Toronto, Canada / Seattle, WA (Hybrid)", True, False, "Hybrid"),
        ("Remote (US/Canada)", True, False, "Remote"),
        ("Hyderabad, India", False, False, ""),
    ],
)
def test_us_locations(location, is_us, unclear, work_type):
    result = classify_location(location)
    assert (result.is_us, result.unclear, result.work_type) == (is_us, unclear, work_type)


def test_discovery_city_disambiguates_city_name():
    assert classify_location("Fort Bliss", "fort-bliss", "tx").is_us
    assert not classify_location("CA", "ca", "").is_us


@pytest.mark.parametrize(
    ("title", "years", "posted", "location", "description", "company", "score", "status", "reasons"),
    [
        (
            "DevOps Engineer I", 2, NOW - timedelta(hours=12), "Austin, TX (Onsite)",
            "E-Verify. Visa sponsorship available.", "Example", 100, "alert",
            {"core title +45", "junior/level I +15", "2 required years +10", "posted within 24h +15",
             "Onsite +5", "E-Verify/OPT-friendly +5", "sponsorship available +5"},
        ),
        (
            "NOC Engineer II", 4, NOW - timedelta(hours=30), "Remote",
            "No OPT or CPT.", "Example Staffing", 5, "quiet",
            {"bridge title +30", "level II +0", "4 required years -5", "posted 24-48h ago +8",
             "Remote -3", "no sponsorship -15", "location unclear -5", "staffing firm -5"},
        ),
        (
            "SRE II", 8, NOW - timedelta(hours=12), "Austin, TX (Hybrid)",
            "U.S. citizenship required.", "Example", 65, "blocked",
            {"core title +45", "level II +0", "8 required years +0", "posted within 24h +15", "Hybrid +5"},
        ),
        (
            "Cloud Engineer", 5, NOW - timedelta(hours=30), "Remote (US)",
            "", "Example", 55, "too_senior",
            {"core title +45", "level unspecified +5", "5 required years +0", "posted 24-48h ago +8", "Remote -3"},
        ),
        (
            "DevOps Engineer", None, None, "Chicago, IL (Onsite)",
            "W2 only.", "Example", 52, "alert",
            {"core title +45", "level unspecified +5", "years unknown +0", "date unknown -3", "Onsite +5"},
        ),
    ],
)
def test_scoring_explains_every_point(title, years, posted, location, description, company, score, status, reasons):
    result = score_job(
        classify_title(title),
        years,
        posted,
        NOW,
        classify_location(location),
        classify_authorization(title, description, company),
    )
    assert result.score == score
    assert result.status == status
    assert set(result.reasons) == reasons
    assert sum(int(reason.split()[-1]) for reason in result.reasons) == score
    if years is None:
        assert "years unknown" in result.flags
    if posted is None:
        assert "date unknown" in result.flags
