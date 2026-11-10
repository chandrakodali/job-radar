from careers_scan.detect import (analyze, decode_cfemail, email_kind, find_emails, match_vendor, slug_matches,
                                 vendor_slug)
from careers_scan.urls import canon, normalize_website, registrable_domain


# ------------------------------------------------------------------ URLs

def test_normalize_website_edge_cases():
    assert normalize_website("acme.com") == "https://acme.com/"
    assert normalize_website("  HTTP://WWW.Acme.COM./about?x=1#top ") == "https://www.acme.com/"
    assert normalize_website("//acme.io/x") == "https://acme.io/"
    assert normalize_website("") is None
    assert normalize_website(None) is None
    assert normalize_website("not a url") is None
    assert normalize_website("ftp://acme.com") is None
    assert normalize_website("mailto:hi@acme.com") is None
    assert normalize_website("localhost") is None
    assert normalize_website("http://10.0.0.1") is None
    assert normalize_website("https://www.linkedin.com/company/acme") is None  # a profile, not a site
    assert normalize_website("https://apps.apple.com/us/app/acme/id1") is None
    assert normalize_website("https://github.com/acme") is None
    assert normalize_website("https://github.com") == "https://github.com/"   # GitHub the company
    assert normalize_website("acme.notion.site/Hello-123") == "https://acme.notion.site/"
    assert normalize_website("münchen-startup.de") == "https://münchen-startup.de/"


def test_registrable_domain_and_private_suffixes():
    assert registrable_domain("https://jobs.acme.co.uk/x") == "acme.co.uk"
    assert registrable_domain("acme.github.io") == "acme.github.io"
    assert registrable_domain("a.vercel.app") != registrable_domain("b.vercel.app")
    assert registrable_domain("") == ""
    assert canon("https://Acme.com/careers/#jobs") == canon("https://acme.com/careers")


# ------------------------------------------------------------------ vendors

def test_vendor_matching_positive():
    cases = {
        "https://boards.greenhouse.io/acme": "greenhouse",
        "https://job-boards.greenhouse.io/acme/jobs/1": "greenhouse",
        "https://boards.greenhouse.io/embed/job_board/js?for=acme": "greenhouse",
        "https://jobs.lever.co/acme": "lever",
        "https://jobs.eu.lever.co/acme": "lever",
        "https://jobs.ashbyhq.com/acme": "ashby",
        "https://apply.workable.com/acme/": "workable",
        "https://acme.bamboohr.com/careers": "bamboohr",
        "https://acme.wd1.myworkdayjobs.com/External": "workday",
        "https://acme.breezy.hr/": "breezy",
        "https://acme.applytojob.com/apply": "jazzhr",
        "https://acme.recruitee.com/": "recruitee",
        "https://acme.teamtailor.com/jobs": "teamtailor",
        "https://acme.jobs.personio.de/": "personio",
        "https://ats.rippling.com/acme/jobs": "rippling",
        "https://www.linkedin.com/company/acme/jobs/": "linkedin_jobs",
        "https://www.ycombinator.com/companies/acme/jobs": "yc_jobs",
        "https://wellfound.com/company/acme/jobs": "wellfound",
        "https://docs.google.com/forms/d/e/abc/viewform": "google_forms",
        "https://forms.gle/AbC123": "google_forms",
        "https://acme.typeform.com/to/xyz": "typeform",
        "https://tally.so/r/abc": "tally",
        "https://acme.notion.site/Careers-0123": "notion",
        "https://www.notion.so/acme/Jobs-123": "notion",
        "https://docs.google.com/document/d/abc/edit": "google_docs",
        "https://app.dover.com/apply/acme/123": "dover",
        "https://acme.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX": "oracle",
    }
    for url, name in cases.items():
        v = match_vendor(url)
        assert v is not None and v.name == name, url


def test_vendor_marketing_and_unrelated_sites_do_not_match():
    for url in ("https://www.greenhouse.io/", "https://greenhouse.io/pricing", "https://www.lever.co/",
                "https://lever.co/careers", "https://support.greenhouse.io/hc", "https://www.workable.com/",
                "https://help.ashbyhq.com/", "https://www.linkedin.com/company/acme", "https://www.ycombinator.com/companies/acme",
                "https://docs.google.com/spreadsheets/d/x", "https://www.bamboohr.com/", "https://acme.bamboohr.com/login",
                "https://app.dover.com/login", "https://www.typeform.com/", "https://acme.com/careers", "not a url", ""):
        assert match_vendor(url) is None, url


def test_vendor_slug_and_company_match():
    assert vendor_slug("https://boards.greenhouse.io/embed/job_board/js?for=acmehq") == "acmehq"
    assert vendor_slug("https://acme.wd5.myworkdayjobs.com/en-US/External") == "acme"
    assert vendor_slug("https://careers-acme.icims.com/jobs") == "acme"
    assert slug_matches("acme", "Acme, Inc.", "acme.com")
    assert slug_matches("acmehq", "Acme", "acme.com")
    assert slug_matches("acme-robotics", "Acme Robotics", "acmerobotics.ai")
    assert not slug_matches("othercorp", "Acme", "acme.com")
    assert not slug_matches("a", "A", "a.com")


# ------------------------------------------------------------------ emails

def test_cloudflare_email_decoding():
    # "careers@acme.com" XOR 0x42, prefixed with the key byte
    key = 0x42
    enc = bytes([key]) + bytes(b ^ key for b in b"careers@acme.com")
    assert decode_cfemail(enc.hex()) == "careers@acme.com"
    assert decode_cfemail("zz") is None
    assert decode_cfemail("") is None


def test_find_emails_obfuscation_and_junk():
    text = ("Write to jobs [at] acme [dot] com or hiring(at)acme.io. Logo: logo@2x.png, icon@3x.webp. "
            "Sentry 0123456789abcdef0123456789abcdef@o1.ingest.sentry.io and react@18.2.0 and user@example.com "
            "and JOBS@ACME.COM again.")
    found = [e for e, _ in find_emails(text)]
    assert "jobs@acme.com" in found
    assert "hiring@acme.io" in found
    assert not any(e.endswith((".png", ".webp")) for e in found)
    assert not any("sentry" in e for e in found)
    assert "user@example.com" not in found
    assert found.count("jobs@acme.com") == 1


def test_email_kind():
    assert email_kind("careers@acme.com", "") == "hiring"
    assert email_kind("jobs@acme.com", "") == "hiring"
    assert email_kind("hr@acme.com", "") == "hiring"
    assert email_kind("talent.team@acme.com", "") == "hiring"
    assert email_kind("hello@acme.com", "send your resume and a short note") == "contextual"
    assert email_kind("hello@acme.com", "questions about our product") is None
    assert email_kind("press@acme.com", "apply for press passes") is None
    assert email_kind("noreply@acme.com", "your application") is None
    assert email_kind("security@acme.com", "careers") is None
    assert email_kind("info@acme.com", "Home About Careers Contact info@acme.com") is None  # footer next to a menu


# ------------------------------------------------------------------ pages

RANE_LIKE = """<html><head><title>Careers | RANE</title></head><body>
<nav><a href="/about">About</a></nav>
<h1>Careers</h1><h2>Open Positions</h2>
<p>Please send your CV along with any other application documents to
<a href="mailto:careers@ranenetwork.com?subject=RANE%20Careers%20Application">careers@ranenetwork.com</a></p>
<p><strong>RANE Enterprise Account Manager</strong></p>
<ul><li>Own client relationships</li></ul>
<p>To apply, please email careers@ranenetwork.com.</p>
<footer>(c) 2026 RANE Network Inc.</footer></body></html>"""


def test_analyze_rane_like_page():
    info = analyze(RANE_LIKE, "https://ranenetwork.com/about/careers")
    assert info.is_careers_like
    assert not info.soft_404 and not info.parked and not info.blocked
    assert [e for e, k, _ in info.emails] == ["careers@ranenetwork.com"]
    assert info.emails[0][1] == "hiring"
    assert "send your CV" in info.apply_by_email_phrase or "To apply" in info.apply_by_email_phrase
    assert "RANE Enterprise Account Manager" in info.roles
    assert info.vendor_links == []


def test_analyze_detects_embeds_in_scripts_and_json():
    page = """<html><title>Join us</title><body><div id="jobs"></div>
    <script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>
    <script id="__NEXT_DATA__">{"apply":"https:\\/\\/jobs.ashbyhq.com\\/acme"}</script></body></html>"""
    info = analyze(page, "https://acme.com/careers")
    names = {l.vendor for l in info.vendor_links}
    assert {"greenhouse", "ashby"} <= names


def test_analyze_soft_404_parked_blocked_js():
    assert analyze("<title>Page not found</title><h1>404</h1>", "https://acme.com/careers").soft_404
    assert analyze("<title>x</title>", "https://acme.com/careers", status=404).soft_404
    assert analyze("<title>acme.com is for sale!</title><p>Buy this domain</p>", "https://acme.com/").parked
    assert analyze("<html><body>x</body></html>", "https://www.hugedomains.com/domain_profile.cfm?d=acme").parked
    cf = "<title>Just a moment...</title><div>Checking your browser</div>"
    assert analyze(cf, "https://acme.com/", status=403).blocked
    assert analyze(cf, "https://acme.com/", status=200).blocked
    normal = "<title>Acme</title><p>" + ("We build things. " * 300) + "</p><script>datadome</script>"
    assert not analyze(normal, "https://acme.com/", status=200).blocked
    shell = '<html><head><title>Acme</title></head><body><div id="root"></div><script src="/app.js"></script></body></html>'
    assert analyze(shell, "https://acme.com/").js_shell


def test_analyze_no_openings_and_engineering():
    info = analyze("<title>Careers</title><p>We currently have no open positions, but software engineers can "
                   "send a resume to hello@acme.com</p>", "https://acme.com/careers")
    assert info.no_openings
    assert info.mentions_engineering
    assert [e for e, k, _ in info.emails] == ["hello@acme.com"]  # generic address next to hiring words


def test_careers_link_scoring_skips_docs_and_blog():
    page = """<title>Acme</title><a href="/careers">Careers</a><a href="/docs/jobs">Jobs API</a>
    <a href="/blog/how-we-hire">How we hire</a><a href="https://jobs.lever.co/acme">We're hiring</a>"""
    info = analyze(page, "https://acme.com/")
    urls = [l.url for _, l in info.careers_links]
    assert urls[0] == "https://acme.com/careers"
    assert "https://acme.com/docs/jobs" not in urls
    assert "https://acme.com/blog/how-we-hire" not in urls


def test_malformed_and_empty_pages_do_not_crash():
    for page in ("", "<<<>>>", "<html><body><a href='http://[::1'>x</a></body>", "\x00\xff garbage <a href=mailto:>"):
        analyze(page, "https://acme.com/careers")


def test_normalize_website_real_data_quirks():
    assert normalize_website("https://pragmaticleaders.io, https://ostronaut.com") == "https://pragmaticleaders.io/"
    assert normalize_website("htps://cloudera.com") == "https://cloudera.com/"
    assert normalize_website("http:/acme.com") == "http://acme.com/".replace("http://", "https://")
    assert normalize_website("https//acme.com") == "https://acme.com/"
    assert normalize_website("https://calendly.com/careers") == "https://calendly.com/"
    assert normalize_website("https://calendly.com/some-user") is None
    assert normalize_website("https://user:pw@acme.com") is None
    assert normalize_website("https://acme.com:99999") is None
