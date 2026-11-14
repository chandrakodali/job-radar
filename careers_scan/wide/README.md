# Wide careers sweep

The sweep uses [Common Crawl](https://commoncrawl.org/) to discover employer careers pages beyond the existing
startup lists. It checks the employer's site for email, form, or document application routes, then ranks matching
infrastructure roles with the radar's profile rules.

- [Roles](ROLES.md) shows matches by profile and how to apply.
- [Summary](SUMMARY.md) shows coverage and the site's classification counts.
- [Matched roles CSV](data/roles.csv) and [companies CSV](data/email_companies.csv) are generated after triage.
- The `wide-data` GitHub release stores the full candidate list, SQLite state, and compressed company export.

In **Actions**, choose a workflow and select **Run workflow**.

| Workflow | When to use it |
| --- | --- |
| `wide-smoke` | Check a real index file, the RANE email canary, and the full pipeline on a small sample. It also runs on relevant pull requests. |
| `wide-sweep` | Discover candidates from one Common Crawl snapshot. Leave `crawl` at `latest` or provide a crawl ID. A nonzero `limit_files` creates a preview artifact. A full sweep saves its coverage and starts triage. |
| `wide-triage` | Check the next set of due sites. Increase `max_sites` to drain a backlog. Set `dry_run` to inspect reports as artifacts. |

The monthly sweep runs on the 6th and triage runs daily. The workflows share one concurrency group because
they both write the state. The per-site schedule gives priority to US and location-unknown sites with technology
roles. A missed read leaves a site due for later.

Coverage is limited to the sites in the crawl and the English careers URL patterns the index search recognizes.
JavaScript-only pages and application instructions on unrelated URLs can be missed. A site marked
`unknown` needs location verification. The results describe the application routes the employer's page exposes.
They do not establish whether an opening also appears on a third-party job board. Archived pages can be stale.

## Add optional sources

From the repository root, use a Python 3.12 environment.

```sh
pip install -r careers_scan/wide/requirements.txt -r requirements.txt
python -m careers_scan.wide.sources hn --out hn-sites.csv
python -m careers_scan.wide.state candidates --db wide-state.sqlite --csv hn-sites.csv --src hn
python -m careers_scan.wide.state candidates --db wide-state.sqlite --csv careers_scan/wide/extra_sites.csv --src owner
```

`extra_sites.csv` is the owner's hand-maintained `domain,url` list. A homepage URL is accepted.

For phrase search, run `python -m careers_scan.wide.sources tavily --out phrase-sites.csv` with
`TAVILY_API_KEY` in the environment. Use a repository secret if you run this command in Actions. The command
rotates 30 basic queries per run, so run it at most once a day to stay within the monthly free allowance.

To import leads from the Department of Labor's LCA disclosure workbook, install `openpyxl`, obtain a workbook
using `scripts/fetch_lca.py` or the [DOL quarterly releases](https://www.dol.gov/agencies/eta/foreign-labor/performance),
then run `python -m careers_scan.wide.sources lca --xlsx FILE.xlsx --out lca-sites.csv`. Only candidate domains
are exported. A point-of-contact domain is a hint, and a past certified filing does not establish current sponsorship.
Feed either CSV to `state candidates` with `--src tavily` or `--src lca`.

The workflow commands also run from a local checkout. Use a dedicated output directory for index and triage
artifacts. Run the state-writing commands one at a time when sharing a database.
