# US-wide sweep for apply-by-email jobs: hand-off plan

Written 2026-11-14 for the agent that continues this work. Branch: `wide-sweep`.

## 0. Rules for the agent doing the work

- Read sections 1-5 once. Then do the work packages in section 6 **in order, one per session**, and stop after each.
- Work only on branch `wide-sweep`. Never commit to `main` and never force-push. `main` gets bot commits about
  every 15 minutes, so before each push run `git fetch --depth=50 origin main` and rebase your own commits.
- New code goes in `careers_scan/wide/` and `careers_scan/tests/test_wide*.py`. Do not edit existing files unless a
  work package names them.
- Run everything from the repo root. Setup: Python 3.12,
  `pip install -r careers_scan/wide/requirements.txt -r requirements.txt`. After every change:
  `python -m pytest careers_scan/tests -q`.
- Match the repo's style: `argparse` CLIs run as `python -m careers_scan.wide.<module>`, type hints, tests that
  never touch the network (fake fetchers, local files).
- Stay polite to websites: always fetch through `careers_scan.fetch.Fetcher` (robots.txt, one request per second
  per host, the identifying User-Agent). Do not scrape LinkedIn or any job site. Do not send email. Do not call
  the Greenhouse / Lever / Ashby APIs from this track (`probe=False`).
- To save tokens: do not open `careers_scan/snapshots/`, and read data CSVs with `head` only. Do not re-research
  the facts in section 2. If a fact or estimate in this plan turns out wrong, fix the plan in the same commit.

## 1. Goal and limits

The owner wants every job in the US that is posted like <https://www.ranenetwork.com/about/career-page>: no Apply
button, no tracking system, only "email your CV to careers@...". Long run times are acceptable; it must be robust.

`careers_scan/` already does this for about 6,264 startups taken from three GitHub lists (481 apply-by-email
companies found, RANE among them) and lists matching roles twice a day in `careers_scan/ROLES.md`. Its limit is
the company list. The wide sweep searches careers pages in Common Crawl beyond those lists.

Limits to state plainly in the docs (WP8), because no system can find literally every such job:

- There is no registry of all US company websites. Coverage is "sites Common Crawl captured that have a
  careers-like URL in English", accumulated over several monthly crawls, plus the optional channels in WP7.
- Pages built entirely with JavaScript, and "send us your CV" text on pages not named like a careers page
  (for example a contact page), are missed.
- "Not on LinkedIn" cannot be proven. What the data shows is that the employer's own site offers no route but
  email (no tracking system, no LinkedIn / Indeed link on its careers page).

## 2. Facts checked on 2026-11-14 (do not re-check)

**Common Crawl** (sources: commoncrawl.org blog "July 2026 crawl archive now available", status.commoncrawl.org,
the `commoncrawl/cc-index-table` schema file):

- One crawl about every month, id `CC-MAIN-<year>-<week>`. July 2026 (`CC-MAIN-2026-30`): 2.14 billion pages,
  40.5 million hosts, 33.2 million registered domains.
- The URL index (formerly "columnar index") is Parquet. File list:
  `https://data.commoncrawl.org/crawl-data/<ID>/cc-index-table.paths.gz` (900 files, 0.20 TiB, three subsets).
  Use only lines containing `/subset=warc/`. Compression is ZStandard from `CC-MAIN-2026-30`, gzip before;
  DuckDB reads both.
- Columns: `url_surtkey url url_host_name url_host_tld url_host_2nd_last_part ... url_host_registry_suffix
  url_host_registered_domain url_host_private_suffix url_host_private_domain url_host_name_reversed url_protocol
  url_port url_path url_query fetch_time fetch_status fetch_redirect content_digest content_mime_type
  content_mime_detected content_charset content_languages content_truncated warc_record_id warc_filename
  warc_record_offset warc_record_length warc_segment crawl subset`.
- Access: download whole files and retry no more than once per second; rate-limit answers from
  `data.commoncrawl.org` are 4xx (often 403), S3 back-pressure is 503. Common Crawl itself recommends downloading
  the Parquet files and querying them locally with DuckDB. `index.commoncrawl.org` (CDX API) is strictly
  rate-limited: never use it for bulk work. Range requests for single archived pages are welcome in moderate
  volume.

**GitHub Actions** (docs.github.com/en/actions/reference/limits): 6 hours per job, 256 matrix jobs per run,
35 days per run, 20 concurrent jobs on the Free plan (shared with the radar workflows, so keep `max-parallel`
at 8-10), `GITHUB_TOKEN` 1,000 API requests per hour, standard runners free for public repositories.

**Search APIs**: Google's Custom Search JSON API is closed to new customers and ends 2027-01-01. Brave removed
its free plan in February 2026 (a $5 monthly credit, card required). Serper gives 2,500 free queries once; Tavily
1,000 a month. Phrase search is therefore optional (WP7), never a dependency.

**This repo**: the existing scan handles about 600 companies per shard-hour, far too slow for a million sites,
hence `HintScanner` below. At commit `376a0ce` two tests in `careers_scan/tests/test_roles.py` fail (see WP0).

## 3. Design

```
monthly  wide-sweep   Common Crawl URL index (about 300 Parquet files)
                      -> ccindex.py: one best careers URL per site      cc-candidates-<crawl>.csv.gz
                      -> state.py: new sites into the SQLite state      wide-state.sqlite.gz
daily    wide-triage  state.py work list (due sites, biggest backlog first)
                      -> triage.py: HintScanner reads each careers page (a few requests per site),
                         falls back to Common Crawl's archived copy when the live site blocks or fails
                      -> state.py: results, tiers, roles
                      -> report.py: roles matched to the radar's profiles, ROLES.md, SUMMARY.md, alerts
```

- **Storage.** Big files are assets of one GitHub release tagged `wide-data` (no git bloat): the state database
  and one candidate list per crawl. Only small reports are committed: `careers_scan/wide/ROLES.md`,
  `SUMMARY.md`, `data/roles.csv`, `data/email_companies.csv`.
- **Both workflows share** `concurrency: group: careers-wide` so only one writes the state at a time.
- **Tiers** (set after each triage; decides how often a site is read again):

  | Tier | Sites | Read again after |
  | --- | --- | --- |
  | 1 | category email / form / doc, `us` not `non_us`, `tech` = 1 | 1 day |
  | 2 | other email / form / doc | 7 days |
  | 3 | `careers_page_unclear`, `no_openings` | 30 days |
  | 4 | `ats`, `ats_probe`, `external_board`, `no_careers_page`, `parked` | 180 days |
  | 5 | `blocked`, `unreachable`, `js_only`, `robots_disallowed` | 30 days, 180 once `fails` >= 3 |
  | 9 | never read | now |

- **WP5 smoke measurements, CC-MAIN-2026-43:** the first of 300 index files yielded 54 candidate domains.
  GitHub triaged all 54 in about 28 seconds (roughly 116 sites per minute). One site was classified as
  email and 18 sites were classified from archive copies. The one-file sample cannot establish total
  crawl coverage, email share, or sustained throughput. Measure those in WP6.

## 4. Status

| Piece | State |
| --- | --- |
| Research, design, facts in section 2 | done |
| `careers_scan/wide/ccindex.py` (index query, URL scoring, download with retry, CLI `paths` / `sweep` / `merge`) | written; query, scoring and merge tested on a synthetic index file; the download and crawl-lookup code has never run against the real service |
| `careers_scan/wide/hint.py` (`HintScanner`, `OnePageFetcher`, `parse_warc_record`, `archived_page`, `us_class`, `guess_name`, `TECH_RE`) | written and tested, except `archived_page`'s network call |
| `careers_scan/tests/test_wide.py` | 5 tests pass |
| `careers_scan/wide/requirements.txt` | written (adds `duckdb`) |
| Everything in section 6 | not started |

## 5. Existing code to reuse (read these, do not rewrite them)

- `careers_scan/detect.py`: `analyze(html, url, status) -> PageInfo` (title, text, is_careers_like, emails, roles,
  careers_links, detail_links, vendor_links, blocked, soft_404, js_shell, mentions_engineering).
- `careers_scan/scan.py`: `Scanner`, `FIELDS` (row columns), `CATEGORIES`, `shard_of(domain, shards)`.
- `careers_scan/fetch.py`: `Fetcher(timeout, max_bytes, host_delay, use_robots).page(url) -> Page(url, status,
  ctype, text, error)`.
- `careers_scan/roles.py`: `extract_roles(html, page_url) -> [PageRole(title, url, page_url, location, emails,
  apply_url)]`, `is_careers_url`, `load_rules(config_path) -> (Rules, cfg)`, `infra_category(title, rules)`,
  `read_job_page`, `assess_role(...)`, `role_key(domain, title)`, `mailto(email, title, company)`.
- `careers_scan/apply_list.py`: the model for scoring roles and writing `ROLES.md` (`build`, `_table`, `markdown`).
- `careers_scan/watch.py` lines 197-215: how alerts are sent (`radar.notify.Notifier({}, dry_run=False)`,
  `.configured`, `.send_text(text, title=, click=)`), capped at 15, never on a first run.
- `.github/workflows/careers-scan.yml`: the pattern for matrix shards, artifacts, merge and the commit-retry loop.

## 6. Work packages

### WP0 - Baseline

1. Install and run the tests. Expect exactly two failures, both in `test_roles.py`:
   `test_extract_roles_keeps_titles_and_their_own_details` and `test_build_and_save_apply_list`.
2. Cause: both expect "Senior Site Reliability Engineer" to be dropped, but title classification keeps it as
   an infrastructure role. The experience check happens later. As documented in the README, senior roles are
   kept when they ask for under 5 years. When experience is unstated, as in this test fixture, the role is
   listed with the flag "senior title, years not stated". Fix the test expectations, not the config.
   `careers-scan.yml` runs these tests before the monthly scan, so failures block the scan on the 1st.

Done when: `python -m pytest careers_scan/tests -q` passes completely.

### WP1 - `careers_scan/wide/state.py` (SQLite state)

Schema (create if missing):

```sql
CREATE TABLE IF NOT EXISTS site(
  domain TEXT PRIMARY KEY, url TEXT NOT NULL, alt_url TEXT DEFAULT '', detail_url TEXT DEFAULT '',
  n_detail INTEGER DEFAULT 0, cc_score INTEGER DEFAULT 0, src TEXT DEFAULT '', warc TEXT DEFAULT '',
  first_seen TEXT DEFAULT '', checked TEXT DEFAULT '', fails INTEGER DEFAULT 0,
  category TEXT DEFAULT '', via TEXT DEFAULT '', name TEXT DEFAULT '', careers_url TEXT DEFAULT '',
  apply_emails TEXT DEFAULT '', apply_phrase TEXT DEFAULT '', form_url TEXT DEFAULT '', doc_url TEXT DEFAULT '',
  ats TEXT DEFAULT '', roles TEXT DEFAULT '[]', tech INTEGER DEFAULT 0, us TEXT DEFAULT '',
  tier INTEGER DEFAULT 9, note TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS site_due ON site(tier, checked);
CREATE TABLE IF NOT EXISTS role(
  key TEXT PRIMARY KEY, domain TEXT, title TEXT, url TEXT, location TEXT DEFAULT '', emails TEXT DEFAULT '',
  apply_url TEXT DEFAULT '', first_seen TEXT, last_seen TEXT, baseline INTEGER DEFAULT 0, assessed TEXT DEFAULT '{}');
CREATE INDEX IF NOT EXISTS role_domain ON role(domain);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
```

CLI:

- `candidates --db DB --csv cc-candidates.csv.gz --src cc:CC-MAIN-2026-43 [--skip-csv careers_scan/data/classified.csv]`
  Insert new sites (`first_seen` = today, tier 9). For known sites update `url`, `alt_url`, `warc`, `cc_score`
  only when the new score is higher, add `n_detail`, and append the source to `src` (`|`-separated, no
  duplicates). Skip domains listed in `--skip-csv` (column `domain`): the existing track already watches them.
  A CSV with only `domain,url` (WP7 channels) must also work.
- `canary --csv cc-candidates.csv.gz --known careers_scan/data/no_job_board.csv`
  Print `sweep found X of Y known apply-by-email sites (Z%)`, where Y = rows with `category` = `email`. Run this
  before the skip list is applied. It is the honest measure of how much the sweep sees.
- `worklist --db DB --out worklist.csv --max N`
  Order: tier 1 due first; then never-read sites by `n_detail` desc, `cc_score` desc; then other due sites,
  oldest `checked` first. "Due" follows the table in section 3. Columns: `domain,url,alt_url,warc,name`.
- `apply --db DB --results DIR`
  Read `triage-*.jsonl.gz` (WP2 output), update the site rows, set `checked` = today and the tier. A failure
  result (tier-5 category) must not erase a good one: if the stored category is email / form / doc, keep the
  stored fields, add 1 to `fails`, and only accept the failure after 5 in a row; any success resets `fails`.
  Roles: for each role in a result, `key = roles.role_key(domain, title)`; insert with `first_seen` = today and
  `baseline` = 1 when this is the first time the site was ever read, else 0; refresh `last_seen`. Delete roles
  not seen for 60 days.
- `stats --db DB` prints counts by tier, category, `us`, `via`.

Tests (`test_wide_state.py`, temp-file databases): insert and update rules, skip list, canary arithmetic,
work-list order and due dates (pass `today` as a parameter), failure-does-not-erase, role baseline flag,
60-day role expiry.

### WP2 - `careers_scan/wide/triage.py`

`python -m careers_scan.wide.triage --worklist worklist.csv --shard I --shards N --out out/triage-I.jsonl.gz
[--workers 48] [--deadline-minutes 300] [--limit N] [--no-archive]`

`triage_one(fetcher, session, item) -> dict`, for a site of this shard (`shard_of(domain, N) == I`):

1. `row, page = HintScanner(fetcher, probe=False, time_budget=45).scan_hint(domain, url, alt_url, name)`.
2. If `row["category"]` is `blocked` or `unreachable`, the item has a `warc` value and archive use is on:
   `html = archived_page(session, warc)`; if it returns text, scan it with
   `HintScanner(OnePageFetcher(url, html), probe=False)` and use that result when its category is email / form /
   doc / ats / careers_page_unclear / no_openings, with `via = "archive"`. Never use the archive for
   `robots_disallowed`. Keep archive requests to 2 per second per process (a lock plus `time.sleep`).
3. When a careers page was read (`page["html"]`): `name = guess_name(...)`, `us = us_class(page["text"])`, and for
   categories email / form / doc / careers_page_unclear / no_openings with `roles.is_careers_url(page["url"])`:
   `roles = extract_roles(page["html"], page["url"])[:40]`, stored as
   `{"t": title, "u": url, "l": location, "e": emails[:2], "a": apply_url}`.
   `tech = 1` when any role title matches `TECH_RE`, or when no roles were read and
   `row["mentions_engineering"] == "yes"`.
4. Return the row's `domain, category, careers_url, apply_emails, apply_phrase, form_url, doc_url, ats, note,
   final_site` plus `name, us, tech, roles, via` (`"live"` unless step 2 applied).

Details:

- Fetcher: `class WideFetcher(Fetcher)` with `timeout=(6, 15)`, `max_bytes=1_500_000` and an overridden
  `_session()` whose `Retry` is `total=1, connect=0, read=0, status=1, backoff_factor=0.5,
  status_forcelist=(429, 503)`. Create a new fetcher for every 2,000 sites: its per-host caches never shrink.
- `ThreadPoolExecutor(max_workers=workers)`. One failing site must never stop the shard (catch, log, emit a
  row with category `unreachable` and the error name in `note`).
- After `--deadline-minutes` stop starting new sites; unread sites simply stay due.
- Write to `<out>.part`, then rename.

Tests (`test_wide_triage.py`): pass a fake fetcher (see `Fake` in `test_wide.py`) into `triage_one`; cover an
email page (roles, `tech`, `us`, name), a blocked page rescued by the archive (monkeypatch `archived_page`),
`robots` not rescued, an exception inside one site, and the deadline.

### WP3 - `careers_scan/wide/report.py` (roles, reports, alerts)

`python -m careers_scan.wide.report --db DB [--profiles config.yaml,profiles/freshers/config.yaml]
[--max-detail 1500] [--dry-run]`

1. Candidate roles: rows of `role` whose site is tier 1 or 2 and not `non_us`.
2. For each profile config: `rules = roles.load_rules(path)[0]`; keep roles where
   `roles.infra_category(title, rules)` returns a category. For a kept role not yet assessed for that profile,
   read its own page once when `url` differs from the site's careers page (`roles.read_job_page`), then
   `roles.assess_role(rules, company, domain, title, category, location, description, url)` and store
   `{profile: {"score", "status", "flags", "location", "outside_us"}}` in `role.assessed`. At most `--max-detail`
   page reads per run, newest roles first; the rest wait for the next run. Follow `apply_list.build` for the
   status logic (`outside_us` when `location_class == "non_us"`).
3. Write:
   - `careers_scan/wide/ROLES.md`: one section per profile, the same table as `apply_list._table`
     (Score, Role, Company, Location, Apply, Since, Notes), mailto links from `roles.mailto`, statuses `alert` and
     `quiet` only, best 500 per profile, 🆕 for roles first seen in the last 2 days. Say in the intro that a
     site marked `us = unknown` did not show a US address.
   - `careers_scan/wide/data/roles.csv`: every matched role.
   - `careers_scan/wide/data/email_companies.csv`: tier 1 and 2 sites that are not `non_us`, columns
     `domain,name,category,us,tech,apply_emails,form_url,doc_url,careers_url,roles_sample,first_seen`
     (no `checked` column, so the file changes only when content does), at most 50,000 rows, tier then name.
   - `careers_scan/wide/SUMMARY.md`: date, crawls swept (from `meta`), counts by category / tier / `us` / `via`,
     the canary line, and the first 200 tier-1 sites.
   - `wide-email-companies.csv.gz` in the working directory: all email / form / doc sites (uploaded as a release
     asset by the workflow).
4. Alerts: roles with `baseline = 0`, first seen today, status `alert` in any profile; at most 15, then one
   "N more" message; skipped with `--dry-run`. Copy the pattern from `watch.py`.

Tests (`test_wide_report.py`): build a small database by hand, stub the fetcher, check the three files and that
baseline roles never alert.

### WP4 - Workflows

Two files, modelled on `careers-scan.yml` (same action versions, same commit-retry loop). Both:
`permissions: {contents: write, actions: write}`, `concurrency: {group: careers-wide, cancel-in-progress: false}`,
`env GH_TOKEN: ${{ github.token }}` wherever `gh` is used, install with
`pip install -r careers_scan/wide/requirements.txt -r requirements.txt`.

`.github/workflows/wide-sweep.yml`: `workflow_dispatch` (inputs `crawl` default `latest`, `limit_files` default
`0`) and `schedule: cron '17 6 6 * *'`.

1. `plan`: run the tests; `crawl=$(python -m careers_scan.wide.ccindex paths --crawl "$CRAWL" --out paths.txt)`;
   create the release if missing (`gh release view wide-data || gh release create wide-data --title "Wide sweep
   data" --notes "Generated files of the US-wide careers sweep."`); output `skip=true` on a scheduled run when
   a completion marker for that crawl already exists; upload `paths.txt` as an artifact.
2. `index` (needs plan, skipped when `skip`): matrix `shard: [0..11]`, `fail-fast: false`, `max-parallel: 8`,
   `timeout-minutes: 300`: `python -m careers_scan.wide.ccindex sweep --paths paths.txt --shard S --shards 12
   --limit-files "$LIMIT" --deadline-minutes 270 --out out/cc-S.csv.gz`; upload `out/` (retention 5 days).
3. `collect` (`if: always()` and not skipped): download the `cc-*` artifacts;
   `ccindex merge --inputs out --out cc-candidates-$CRAWL.csv.gz`; write "index files read: X of Y" to the job
   summary from the `*.done` files; `state canary`; download and gunzip `wide-state.sqlite.gz` if the asset
   exists; `state candidates ... --src cc:$CRAWL --skip-csv careers_scan/data/classified.csv`; gzip and
   `gh release upload wide-data wide-state.sqlite.gz cc-candidates-$CRAWL.csv.gz --clobber`;
   then `gh workflow run wide-triage.yml -f max_sites=400000 -f shards=10`.
   Publish only a complete sweep. A limited run uploads a candidate preview artifact.

`.github/workflows/wide-triage.yml`: `workflow_dispatch` (inputs `max_sites` default `60000`, `shards` default
`4`, `dry_run` boolean) and `schedule: cron '29 3 * * *'`.

1. `plan`: download the state (exit cleanly with a notice when there is none yet); `state worklist --max`;
   upload it; output the matrix as JSON
   (`python -c "import json,sys; print(json.dumps(list(range(int(sys.argv[1])))))" "$SHARDS"`).
2. `triage`: `matrix.shard: ${{ fromJSON(needs.plan.outputs.shards) }}`, `fail-fast: false`, `max-parallel: 10`,
   `timeout-minutes: 345`. Run three processes per job to use the runner's cores, then `wait`: for `k` in 0 1 2,
   `python -m careers_scan.wide.triage --worklist worklist.csv --shard $((S + N*k)) --shards $((N*3))
   --deadline-minutes 320 --out out/triage-$S-$k.jsonl.gz &`. Upload `out/`.
3. `merge` (`if: always()`): download the state again (fresh) and the results; `state apply`; `report`
   (pass the notification secrets exactly as `careers-watch.yml` does); unless `dry_run`: upload the state and
   `wide-email-companies.csv.gz` with `--clobber`, then commit `careers_scan/wide/ROLES.md`, `SUMMARY.md` and
   `data/` with the repo's rebase-retry loop.

### WP5 - Smoke test on GitHub (first contact with the real services)

Real Parquet uses a timezone-aware timestamp. Cast it to text in DuckDB before fetching Python rows.
The initial smoke artifact contained one email-classified domain. Its page did give email application
instructions, but described a Toronto clinic that was marked location-unknown. Explicit Canadian city
location phrases now count as non-US when the page has no US signal.

Add `.github/workflows/wide-smoke.yml`, triggered by `pull_request` on `careers_scan/wide/**` and
`.github/workflows/wide-*.yml`, plus `workflow_dispatch`. One job, about 15 minutes, writes nothing to the release:

1. Tests.
2. Canary A: triage a one-line work list (`ranenetwork.com`,
   `https://www.ranenetwork.com/about/career-page`); fail the job unless the result is category `email` with
   `careers@ranenetwork.com`.
3. `ccindex paths --crawl latest`; `ccindex sweep --shards 300 --shard 0 --limit-files 1`; a fresh database;
   `state candidates`; `state worklist --max 300`; `triage`; `state apply`; `report --dry-run`.
4. Put in the job summary: sites found in that one index file, triage sites per minute, counts by category,
   and upload all outputs as an artifact.

Open the pull request from `wide-sweep` to `main` so this runs. Read the log, fix what the real services show
(this is where untested code meets Common Crawl), and replace the estimates in section 3 with the measured
numbers. Then read 30 of the sites classified `email` by hand in the artifact: if more than 3 are wrong,
tighten the rule that let them in and add a test for it.

### WP6 - First full run (after the owner merges the pull request)

1. Run `wide-sweep` by hand. Check the canary line: the share of the 481 known apply-by-email sites the sweep
   found. If it is under 50%, sweep the two previous crawls as well (`-f crawl=<id>`); coverage adds up
   because each crawl samples different pages.
2. Let `wide-triage` drain the backlog (run it by hand with larger `max_sites` if the daily slice is too slow).
3. Tune from real data: tier-1 size (if the daily re-read does not fit in one run, narrow `TECH_RE` or re-read
   tier 1 every 2 days), `DENY` list (add any job site that shows up often), and the weak segments left out of
   `SEG` on purpose (`join`, `positions`, `opportunities`): add them only if precision stays acceptable.

### WP7 - More ways to find companies (each is optional and independent)

Each channel writes a CSV with `domain,url` and feeds `state candidates --src <name>`. A homepage URL is a fine
hint: `HintScanner` follows a link labelled "Careers" from a page that is not a careers page.

- **Hacker News "Who is hiring?"** (no key): the last 6 monthly threads through the Algolia API already used in
  `radar/sources.py` (`HN_SEARCH`, `HN_ITEM`). Take link and email domains from each top-level comment.
- **The owner's own list**: `careers_scan/wide/extra_sites.csv`, never overwritten.
- **Phrase search** (needs a key in a repository secret; skip silently when absent): queries such as
  `"send your resume to" "devops engineer"`, `"email your resume" careers "site reliability"`,
  `"to apply" "email" "cloud engineer" -site:linkedin.com -site:indeed.com`, rotated so about 30 run per day.
  Write one small adapter per provider and check the provider's current documentation and free allowance before
  coding; section 2 lists what was true in November 2026.
- **US Department of Labor LCA data**: `scripts/fetch_lca.py` already downloads the quarterly file. If its header
  has an employer contact email column, the email domains of employers that filed for the profile's job titles
  are company websites of employers that sponsor visas. Check the header first; this column is unverified.
- **IRS Form 990 e-file data** lists nonprofit websites (`WebsiteAddressTxt`). Large; do it last, if at all.

### WP8 - Documentation

- `careers_scan/wide/README.md`: what it does, how to run each workflow, where results are, the limits from
  section 1, and that results come from Common Crawl (credit it by name with a link).
- Add a short section to the root `README.md` and to `careers_scan/README.md` linking to
  `careers_scan/wide/ROLES.md` and `SUMMARY.md`.

## 7. Finished means

- `python -m pytest careers_scan/tests -q` passes.
- The smoke workflow passes on the pull request, including canary A.
- After the first full run: `SUMMARY.md` shows the canary share and the counts; `ROLES.md` lists roles with
  working mailto links; the daily run finishes inside its time limit; the release holds the state and the
  candidate list.

## 8. Known risks

| Risk | What to do |
| --- | --- |
| Common Crawl throttles (403 / 503) | Already handled by `download()` (waits, resumes). Lower `max-parallel` of `index` if a whole shard times out. |
| GitHub runner addresses are blocked by some sites | The archive fallback in WP2 classifies them from Common Crawl's copy. |
| GitHub flags heavy crawling from Actions | Everything is rate-limited and resumable; the same commands run on any computer if Actions ever becomes unavailable. Say so in the README. |
| Membership or newsletter pages at `/join-us` counted as careers pages | They end as `careers_page_unclear` (tier 3) unless they name a hiring address; check during WP5's hand review. |
| The daily run grows past its time limit | Tiers plus `--max`; unread sites stay due. |
| Two runs write the state at once | The shared concurrency group; never remove it. |
