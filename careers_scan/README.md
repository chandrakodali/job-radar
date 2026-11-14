# Companies that hire without a job board

Some companies post jobs as plain text on their own website and ask you to email a CV (for example
[RANE](https://ranenetwork.com/about/careers)). Job aggregators read Greenhouse, Lever, Ashby and other
applicant tracking systems, so they never see these roles. This folder finds those companies and
watches their careers pages.

**[Roles to apply for now →](ROLES.md)** · [Companies found →](SUMMARY.md) · [Careers page changes →](CHANGES.md)

## How it works

1. **Company list** (`sources.py`). About 6,000 companies from public GitHub datasets, merged by domain:
   - every active YC company ([yc-oss/api](https://github.com/yc-oss/api))
   - commercial open-source companies ([Awesome-OSS-Companies](https://github.com/Chinstrap-Community/Awesome-OSS-Companies))
   - remote-friendly companies ([remoteintech/remote-jobs](https://github.com/remoteintech/remote-jobs))
   - your own list: [`companies_extra.csv`](companies_extra.csv)
2. **Scan** (`scan.py`, monthly). For each company it opens the homepage, follows links labelled careers / jobs /
   join us (in several languages), tries common paths (`/careers`, `/jobs`, `/about/careers`, `careers.` subdomain…)
   and, when a listing shows no apply route, opens up to three individual job pages. Each company is put in one
   category, most decisive first:

   | Category | Meaning |
   | --- | --- |
   | `ats` | Uses an applicant tracking system (60+ recognised: Greenhouse, Lever, Ashby, Workday, BambooHR, Workable…) |
   | `email` | **No job board: apply by email** (careers@, jobs@, or an address next to "send your CV") |
   | `form` | **No job board: Google Form, Typeform, Tally, Airtable…** |
   | `doc` | **No job board: jobs posted as a Notion page or Google Doc** |
   | `ats_probe` | Nothing on the site, but a matching board exists on Greenhouse / Lever / Ashby |
   | `external_board` | Jobs only on LinkedIn, Wellfound, YC Work at a Startup, Indeed… |
   | `careers_page_unclear` / `no_openings` | Careers page found, apply route not in its HTML / says no openings |
   | `no_careers_page`, `js_only`, `blocked`, `parked`, `unreachable`, `robots_disallowed`, `no_website` | Could not tell, and why |

3. **Hand-off to the radar** (`radar_link.py`, after each scan). Companies that turn out to use Greenhouse, Lever,
   Ashby, Workable, Workday and other systems the radar reads, and that the radar didn't know, go into
   `data/radar_boards.csv`. The radar checks those boards on its usual rotation, so their DevOps / SRE roles show
   up in [REPORT.md](../REPORT.md) and your alerts with the same filters and scores.
4. **Roles from no-job-board pages** (`watch.py` + `roles.py` + `apply_list.py`, twice a day). Re-reads the careers
   page of every `email`, `form`, `doc`, `no_openings` and `careers_page_unclear` company, picks out the job
   titles, keeps the cloud / DevOps / SRE / platform / infrastructure ones using the radar's title rules in
   [config.yaml](../config.yaml), opens each role's own page, and scores it with the radar's checks (years of
   experience, visa sponsorship, clearance, US location). The result is [ROLES.md](ROLES.md): one table with an
   ✉ link per role that opens an email to the hiring address with the subject filled in. A phone alert goes out
   for each new matching role (never on the first run). Page changes are still logged in `CHANGES.md`.

## Files

| File | What it is |
| --- | --- |
| `ROLES.md`, `data/roles.csv` | Open cloud / DevOps / SRE / platform / infra roles at no-job-board companies, with apply links |
| `data/radar_boards.csv` | Tracking-system boards handed to the radar |
| `SUMMARY.md` | Counts per category and the no-job-board list (engineering pages first) |
| `data/no_job_board.csv` | Companies that hire by email, form or document, with apply addresses and roles seen |
| `data/check_by_hand.csv` | Careers pages whose apply route wasn't visible (often built with JavaScript) |
| `data/classified.csv` | Every company and its category |
| `data/watchlist.csv` | Pages the daily watcher checks |
| `CHANGES.md`, `data/changes_log.csv` | Latest changes and the full history of changes |
| `companies_extra.csv` | Your own companies (never overwritten) |

## Run it

- Whole scan: **Actions → careers-scan → Run workflow** (about an hour; 10 parallel shards).
  Set *limit* to e.g. `20` for a quick test, and untick *save* to keep results out of the repo.
- Roles and watcher: **Actions → careers-watch → Run workflow** (runs twice a day on its own).
- Locally: `pip install -r careers_scan/requirements.txt`, then
  `python -m careers_scan.scan --companies careers_scan/data/companies.csv --out /tmp/out.jsonl --limit 20`.
  Tests: `python -m pytest careers_scan/tests -q`.

## Good to know

- It is polite: it reads `robots.txt` and skips what it disallows, sends at most one request per second to a
  site, looks at no more than 12 pages per company, and identifies itself in its User-Agent.
- It reads HTML only (no browser), so careers pages built entirely with JavaScript end up in
  `check_by_hand.csv` rather than being guessed, and their roles can't be listed.
- Which titles count, and which are skipped as too senior, comes from the radar's `config.yaml`: change it there
  and both lists follow.
- Addresses are taken only from careers and job pages, or when they're clearly hiring inboxes (careers@, jobs@,
  hr@…). A generic address (info@, hello@) counts only when the text around it mentions applying, a CV or a résumé.
- No list can be complete: companies outside these datasets are found only if you add them to
  `companies_extra.csv`.

## Wide careers sweep

The [wide sweep](wide/README.md) uses Common Crawl to find employer careers pages beyond the company lists.
See the [wide roles report](wide/ROLES.md) and [coverage summary](wide/SUMMARY.md) after triage.
