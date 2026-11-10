import json

from careers_scan.fetch import Page
from careers_scan.scan import Company, Scanner, company_key, shard_of

NOT_FOUND = "<html><head><title>404 Not Found</title></head><body><h1>Not Found</h1></body></html>"


class FakeFetcher:
    """Serves pages from a dict. Values: HTML string, Page, or callable(url) -> Page. Missing URLs are 404."""

    def __init__(self, pages: dict, api: dict | None = None):
        self.pages = {self._k(k): v for k, v in pages.items()}
        self.api = api or {}
        self.calls: list[str] = []

    @staticmethod
    def _k(u: str) -> str:
        return u.rstrip("/").lower()

    def page(self, url: str, robots: bool = True) -> Page:
        self.calls.append(url)
        v = self.pages.get(self._k(url))
        if v is None:
            return Page(url=url, status=404, ctype="text/html", text=NOT_FOUND)
        if callable(v):
            return v(url)
        if isinstance(v, Page):
            return v
        return Page(url=url, status=200, ctype="text/html", text=v)

    def json(self, url: str):
        v = self.api.get(url)
        return (200, v) if v is not None else (404, None)


def run(pages, company=None, api=None, probe=True):
    c = company or Company(name="Acme", website="https://acme.com")
    f = FakeFetcher(pages, api)
    return Scanner(f, probe=probe).scan(c), f


HOME = '<html><title>Acme</title><body><a href="/careers">Careers</a><a href="/about">About</a></body></html>'


def test_rane_style_email_careers_page():
    pages = {
        "https://ranenetwork.com/": '<title>RANE</title><a href="/about/careers">Careers</a>',
        "https://ranenetwork.com/about/careers": """<title>Careers</title><h1>Careers</h1><h2>Open Positions</h2>
            <p>Please send your CV along with any other application documents to careers@ranenetwork.com</p>
            <p><strong>Senior DevOps Engineer</strong></p>""",
    }
    row, _ = run(pages, Company(name="RANE", website="ranenetwork.com"))
    assert row["category"] == "email"
    assert row["apply_emails"] == "careers@ranenetwork.com"
    assert row["careers_url"] == "https://ranenetwork.com/about/careers"
    assert row["mentions_engineering"] == "yes"
    assert "Senior DevOps Engineer" in row["roles_sample"]
    json.dumps(row)  # serialisable


def test_careers_link_straight_to_ats_stops_early():
    pages = {"https://acme.com/": '<title>Acme</title><a href="https://jobs.lever.co/acme">Careers</a>'}
    row, f = run(pages)
    assert row["category"] == "ats" and row["ats"] == "lever"
    assert len(f.calls) == 1  # no need to guess paths


def test_ats_embed_on_careers_page():
    pages = {"https://acme.com/": HOME,
             "https://acme.com/careers": '<title>Careers</title><div id="grnhse_app"></div>'
                                         '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>'
                                         '<p>Questions? careers@acme.com</p>'}
    row, _ = run(pages)
    assert row["category"] == "ats" and row["ats"] == "greenhouse"
    assert row["apply_emails"] == "careers@acme.com"  # recorded, but the tracking system wins


def test_careers_path_redirects_to_ats():
    pages = {"https://acme.com/": "<title>Acme</title><p>We make widgets.</p>",
             "https://acme.com/careers": Page(url="https://jobs.ashbyhq.com/acme", status=200, text="<title>Jobs</title>")}
    row, _ = run(pages)
    assert row["category"] == "ats" and row["ats"] == "ashby"


def test_form_and_doc_routes():
    pages = {"https://acme.com/": HOME,
             "https://acme.com/careers": '<title>Careers</title><h2>Backend Engineer</h2>'
                                         '<a href="https://forms.gle/abc123">Apply here</a>'}
    row, _ = run(pages)
    assert row["category"] == "form" and row["form_url"] == "https://forms.gle/abc123"
    pages = {"https://acme.com/": '<title>Acme</title><a href="https://acme.notion.site/Jobs-0123">Careers</a>'}
    row, _ = run(pages)
    assert row["category"] == "doc" and "notion.site" in row["doc_url"]


def test_hiring_email_in_footer_without_careers_page():
    pages = {"https://acme.com/": "<title>Acme</title><footer>Join us: jobs@acme.com</footer>"}
    row, _ = run(pages)
    assert row["category"] == "email" and row["apply_emails"] == "jobs@acme.com"


def test_generic_email_on_homepage_is_not_an_apply_route():
    pages = {"https://acme.com/": "<title>Acme</title><footer>hello@acme.com</footer>"}
    row, _ = run(pages)
    assert row["category"] == "no_careers_page"
    assert row["apply_emails"] == ""


def test_linkedin_only():
    pages = {"https://acme.com/": HOME,
             "https://acme.com/careers": '<title>Careers</title><p>See our openings on '
                                         '<a href="https://www.linkedin.com/company/acme/jobs/">LinkedIn</a></p>'}
    row, _ = run(pages)
    assert row["category"] == "external_board" and row["board"] == "linkedin_jobs"


def test_js_careers_page_falls_back_to_ats_probe():
    shell = '<html><title>Careers</title><body><div id="root"></div><script src="/a.js"></script></body></html>'
    pages = {"https://acme.com/": HOME, "https://acme.com/careers": shell}
    api = {"https://boards-api.greenhouse.io/v1/boards/acme": {"name": "Acme Inc"}}
    row, _ = run(pages, api=api)
    assert row["category"] == "ats_probe" and row["ats"] == "greenhouse"


def test_probe_rejects_board_of_a_different_company():
    shell = '<html><title>Careers</title><body><div id="root"></div></body></html>'
    pages = {"https://acme.com/": HOME, "https://acme.com/careers": shell}
    api = {"https://boards-api.greenhouse.io/v1/boards/acme": {"name": "Totally Different Corp"},
           "https://api.ashbyhq.com/posting-api/job-board/acme": {"jobs": []},
           "https://api.lever.co/v0/postings/acme?mode=json&limit=1": []}
    row, _ = run(pages, api=api)
    assert row["category"] == "careers_page_unclear"
    assert "JavaScript" in row["note"]


def test_no_openings():
    pages = {"https://acme.com/": HOME,
             "https://acme.com/careers": "<title>Careers</title><p>There are no open positions at the moment.</p>"}
    row, _ = run(pages, probe=False)
    assert row["category"] == "no_openings"


def test_parked_blocked_unreachable_robots():
    row, _ = run({"https://acme.com/": "<title>acme.com is for sale</title><p>Buy this domain today</p>"})
    assert row["category"] == "parked"
    blocked = Page(url="https://acme.com/", status=403, text="<title>Just a moment...</title>")
    row, _ = run({"https://acme.com/": blocked})
    assert row["category"] == "blocked"

    def dns(url):
        return Page(url=url, error="dns")
    row, f = run({"https://acme.com/": dns, "https://www.acme.com/": dns})
    assert row["category"] == "unreachable" and "dns" in row["note"]
    assert "https://www.acme.com/" in f.calls  # tried the www variant

    row, _ = run({"https://acme.com/": Page(url="https://acme.com/", error="robots")})
    assert row["category"] == "robots_disallowed"


def test_www_and_http_fallbacks():
    pages = {"https://acme.com/": Page(url="https://acme.com/", error="dns"),
             "https://www.acme.com/": "<title>Acme</title><footer>careers@acme.com</footer>"}
    row, _ = run(pages)
    assert row["category"] == "email"
    pages = {"https://acme.com/": Page(url="https://acme.com/", error="ssl"),
             "http://acme.com/": "<title>Acme</title><footer>careers@acme.com</footer>"}
    row, _ = run(pages)
    assert row["category"] == "email"


def test_soft_404_and_redirect_home_are_ignored():
    pages = {"https://acme.com/": "<title>Acme</title><p>Widgets</p>",
             "https://acme.com/careers": "<title>Page not found</title><p>Oops</p>",
             "https://acme.com/jobs": Page(url="https://acme.com/", status=200, text="<title>Acme</title>")}
    row, _ = run(pages, probe=False)
    assert row["category"] == "no_careers_page"


def test_recruiting_tool_homepage_linking_customer_boards_is_not_ats():
    pages = {"https://hiretool.io/": '<title>HireTool</title><p>Customers:</p>'
                                     '<a href="https://jobs.lever.co/othercorp">See OtherCorp</a>'
                                     '<a href="https://boards.greenhouse.io/bigco">BigCo</a>'}
    row, _ = run(pages, Company(name="HireTool", website="https://hiretool.io"), probe=False)
    assert row["category"] == "no_careers_page"


def test_follows_job_detail_pages_to_find_email():
    pages = {"https://acme.com/": HOME,
             "https://acme.com/careers": '<title>Careers</title><h2>Open roles</h2>'
                                         '<a href="/careers/platform-engineer">Platform Engineer</a>',
             "https://acme.com/careers/platform-engineer": "<title>Platform Engineer</title><p>Send your resume "
                                                            "to hr@acme.com</p>"}
    row, _ = run(pages)
    assert row["category"] == "email" and row["apply_emails"] == "hr@acme.com"


def test_landing_page_links_to_listing():
    pages = {"https://acme.com/": HOME,
             "https://acme.com/careers": '<title>Life at Acme</title><a href="/careers/open-positions">See open positions</a>',
             "https://acme.com/careers/open-positions": "<title>Open positions</title><h3>SRE</h3>"
                                                        "<p>To apply, email your CV to work@acme.com</p>"}
    row, _ = run(pages)
    assert row["category"] == "email" and row["apply_emails"] == "work@acme.com"


def test_source_careers_url_hint():
    # Hint is a job board but the live site asks for email: the live site wins.
    pages = {"https://acme.com/": HOME,
             "https://acme.com/careers": "<title>Careers</title><p>Send your CV to careers@acme.com</p>"}
    row, _ = run(pages, Company(name="Acme", website="acme.com", careers_url="https://jobs.lever.co/acme"))
    assert row["category"] == "email"
    # Hint is a job board and the site shows nothing: falls back to the hint.
    row, _ = run({"https://acme.com/": "<title>Acme</title>"},
                 Company(name="Acme", website="acme.com", careers_url="https://jobs.lever.co/acme"), probe=False)
    assert row["category"] == "ats" and "source data" in row["note"]
    # Hint is a plain page on another site.
    pages = {"https://acme.com/": "<title>Acme</title>",
             "https://acmejobs.example.org/positions": "<title>Positions</title><p>Email jobs@acme.com</p>"}
    row, _ = run(pages, Company(name="Acme", website="acme.com", careers_url="acmejobs.example.org/positions"))
    assert row["category"] == "email"


def test_no_website_and_profile_websites():
    row, _ = run({}, Company(name="Ghost", website=""))
    assert row["category"] == "no_website" and row["domain"].startswith("name:")
    row, _ = run({}, Company(name="Ghost", website="https://www.linkedin.com/company/ghost"))
    assert row["category"] == "no_website"


def test_off_site_redirect_of_guessed_path_is_ignored():
    pages = {"https://acme.com/": "<title>Acme</title>",
             "https://acme.com/careers": Page(url="https://www.hugedomains.com/x", status=200,
                                              text="<title>Careers</title><p>jobs@acme.com</p>")}
    row, _ = run(pages, probe=False)
    assert row["category"] == "no_careers_page"


def test_website_moved_to_new_domain():
    pages = {"https://oldname.com/": Page(url="https://newname.com/", status=200, text=HOME.replace("Acme", "NewName")),
             "https://newname.com/careers": "<title>Careers</title><p>Email careers@newname.com</p>"}
    row, _ = run(pages, Company(name="OldName", website="oldname.com"))
    assert row["category"] == "email" and row["final_site"] == "newname.com"


def test_page_budget_is_respected():
    many = "<title>Acme</title>" + "".join(f'<a href="/careers{i}">Careers {i}</a>' for i in range(50))
    row, f = run({"https://acme.com/": many}, probe=False)
    assert row["pages_checked"] <= 12


def test_company_key_and_shards_are_stable():
    assert company_key(Company("A", "https://www.acme.com/x")) == "acme.com"
    assert company_key(Company("A", "", careers_url="https://jobs.acme.com")) == "acme.com"
    assert company_key(Company("Some Co", "")) == "name:someco"
    assert {shard_of("acme.com", 10) for _ in range(5)} == {shard_of("acme.com", 10)}
    assert 0 <= shard_of("acme.com", 10) < 10


def test_company_with_only_a_job_board_link():
    c = Company(name="Swif.ai", website="", careers_url="https://www.ycombinator.com/companies/swif-ai/jobs")
    assert company_key(c) == "yc_jobs:swif-ai"
    row, f = run({}, c)
    assert row["category"] == "external_board" and row["domain"] == "yc_jobs:swif-ai"
    assert f.calls == []


def test_js_careers_page_with_footer_address_checks_ats_first():
    shell = ('<html><title>Careers</title><body><div id="root"></div><script src="/a.js"></script>'
             '<footer>careers@acme.com</footer></body></html>')
    pages = {"https://acme.com/": HOME, "https://acme.com/careers": shell}
    api = {"https://api.ashbyhq.com/posting-api/job-board/acme": {"jobs": [{"id": 1}]}}
    row, _ = run(pages, api=api)
    assert row["category"] == "ats_probe" and row["ats"] == "ashby"
    row, _ = run(pages, api={})
    assert row["category"] == "email"


def test_footer_info_address_next_to_careers_menu_is_not_email_route():
    pages = {"https://acme.com/": HOME,
             "https://acme.com/careers": "<title>Careers</title><p>Life at Acme is great.</p>"
                                         "<footer><a href='/careers'>Careers</a> info@acme.com</footer>"}
    row, _ = run(pages, probe=False)
    assert row["category"] == "careers_page_unclear"


def test_real_world_false_positives_from_first_run():
    # A product page about "job descriptions" is not a careers page.
    pages = {"https://adaface.com/": '<title>Adaface</title><a href="/job-descriptions/">Job Descriptions</a>',
             "https://adaface.com/job-descriptions/": "<title>Job description templates</title>"
                                                      "<p>Write a resume screen. Contact deepti@adaface.com</p>"}
    row, _ = run(pages, Company("Adaface", "adaface.com"), probe=False)
    assert row["category"] == "no_careers_page"
    # support@ on a page linked as "Join us" is not an apply route.
    pages = {"https://10by10.io/": '<title>10by10</title><a href="/contact/">Join us</a>',
             "https://10by10.io/contact/": "<title>Contact</title><p>Apply questions? support@10by10.io</p>"}
    row, _ = run(pages, Company("10 By 10", "10by10.io"), probe=False)
    assert row["category"] != "email"
    # A generic address on someone else's domain doesn't count; one on the company's domain does.
    pages = {"https://albaorbital.com/": HOME.replace("Acme", "Alba"),
             "https://albaorbital.com/careers": "<title>Careers</title><p>To apply send your CV.</p>"
                                                "<p>Order kits: sales-team@pocketqubeshop.com</p>"}
    row, _ = run(pages, Company("Alba Orbital", "albaorbital.com"), probe=False)
    assert row["category"] != "email"
    pages["https://albaorbital.com/careers"] += "<p>Applications: tom@albaorbital.com or tom.alba@gmail.com</p>"
    row, _ = run(pages, Company("Alba Orbital", "albaorbital.com"), probe=False)
    assert row["category"] == "email" and "tom@albaorbital.com" in row["apply_emails"]
    # "Investment opportunities" is not a careers link.
    pages = {"https://abatable.com/": '<title>Abatable</title><a href="/investors">Investment opportunities</a>',
             "https://abatable.com/investors": "<title>Investors</title><p>Opportunities for investors.</p>"}
    row, _ = run(pages, Company("Abatable", "abatable.com"), probe=False)
    assert row["category"] == "no_careers_page"


def test_more_small_ats_vendors():
    pages = {"https://aivitex.com/": '<title>Aivitex</title><a href="https://iuhhoo.workwise.io/">Careers</a>'}
    row, _ = run(pages, Company("Aivitex", "aivitex.com"))
    assert row["category"] == "ats" and row["ats"] == "workwise"
